# v3 Operations Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a mobile-first "operations view" dashboard that gives a non-technical, at-a-glance understanding of all OpenClaw agents, their health, scheduled jobs, and server status.

**Architecture:** Single self-contained HTML file (`index-v3.html`) following the existing v1/v2 pattern. No build step, no framework, no external dependencies. Reuses the existing `server.py` and `data.json` pipeline — only small additions to `refresh.sh` to pass through `agentId` on cron jobs and include agent identity descriptions. All styling via CSS custom properties from `themes.json`.

**Tech Stack:** Vanilla HTML/CSS/JS, Python stdlib server, existing `data.json` API

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `index-v3.html` | **Create** | Entire v3 frontend — HTML structure, CSS, JS, all in one file |
| `refresh.sh` | **Modify** (lines ~280-320, cron section) | Add `agentId` and `description` fields to cron output; add `agentIdentities` map |
| `server.py` | **No changes** | All existing API endpoints work as-is |

---

## Design Principles

1. **Mobile-first**: Designed for a phone screen. Single column, large touch targets, no horizontal scroll.
2. **Traffic light UX**: Green = healthy, amber = warning, red = broken. Glanceable in 2 seconds.
3. **Non-technical language**: "Last ran 2h ago" not "lastRunAtMs: 1774162844937". "Working" not "exit code 0".
4. **Progressive disclosure**: Summary first, tap to expand details. Never show everything at once.
5. **Dark theme default**: Matches existing midnight theme. Supports all 6 themes via CSS vars.
6. **60-second auto-refresh**: Same cycle as v2, reuses `/api/refresh`.

---

## Task 1: Enhance refresh.sh — Add agent identities and cron agentId

**Files:**
- Modify: `/home/dawsmana/Documents/openclaw-dashboard/refresh.sh` (cron section, ~line 280-320)

The current `crons` array in data.json lacks `agentId` and `description`. The `agentConfig.agents` array lacks identity descriptions. We need both for the v3 agent cards.

- [ ] **Step 1: Find the cron output section in refresh.sh**

Search for where `crons` array is built. It reads from `jobs.json` but only outputs `name`, `schedule`, `enabled`, `lastRun`, `lastStatus`, `lastDurationMs`, `nextRun`, `model`. We need to add `agentId`.

- [ ] **Step 2: Add `agentId` to cron output**

In the Python heredoc inside refresh.sh, find the cron job loop and add the `agentId` field from each job object:

```python
# In the cron loop, add to the output dict:
"agentId": job.get("agentId", ""),
```

- [ ] **Step 3: Add `agentIdentities` to data.json output**

Add a new top-level key that maps agent IDs to their identity info (read from IDENTITY.md files):

```python
# After agent config section, add identity reader
# Derive agent_ids from the already-parsed agent_list variable
agent_ids = [ag.get('id', '') for ag in agent_list if ag.get('id')]
agent_identities = {}
for agent_id in agent_ids:
    if agent_id == "main":
        id_path = os.path.expanduser("~/.openclaw/workspace/IDENTITY.md")
    else:
        id_path = os.path.expanduser(f"~/.openclaw/workspaces/{agent_id}/IDENTITY.md")
    try:
        with open(id_path) as f:
            lines = f.read().strip().split("\n")
        info = {}
        for line in lines:
            if line.startswith("- **Name:**"):
                info["name"] = line.split(":**")[1].strip()
            elif line.startswith("- **Creature:**"):
                info["creature"] = line.split(":**")[1].strip()
            elif line.startswith("- **Emoji:**"):
                info["emoji"] = line.split(":**")[1].strip()
            elif line.startswith("- **Vibe:**"):
                info["vibe"] = line.split(":**")[1].strip()
        # Get tagline (last non-empty line after ---)
        after_hr = False
        for line in lines:
            if line.strip() == "---":
                after_hr = True
            elif after_hr and line.strip():
                info["tagline"] = line.strip()
                break
        agent_identities[agent_id] = info
    except FileNotFoundError:
        pass

# Add to output dict:
# "agentIdentities": agent_identities,
```

- [ ] **Step 4: Add per-agent `lastActivity` timestamp**

Scan each agent's session directory for the most recently modified JSONL file:

```python
import glob
agent_last_activity = {}
for agent_id in agent_ids:
    pattern = os.path.expanduser(f"~/.openclaw/agents/{agent_id}/sessions/*.jsonl")
    files = glob.glob(pattern)
    if files:
        newest = max(files, key=os.path.getmtime)
        agent_last_activity[agent_id] = int(os.path.getmtime(newest) * 1000)
    else:
        agent_last_activity[agent_id] = 0

# Merge into agentIdentities:
for aid, ts in agent_last_activity.items():
    if aid in agent_identities:
        agent_identities[aid]["lastActivityMs"] = ts
```

- [ ] **Step 5: Test refresh.sh produces the new fields**

Run: `cd /home/dawsmana/Documents/openclaw-dashboard && bash refresh.sh && python3 -c "import json; d=json.load(open('data.json')); print('agentIdentities' in d, len(d.get('agentIdentities',{}))); print(d['crons'][0].get('agentId','MISSING'))"`

Expected: `True 11` (or similar count) and an agent ID like `main`.

- [ ] **Step 6: Commit**

```bash
cd /home/dawsmana/Documents/openclaw-dashboard
git add refresh.sh
git commit -m "feat: add agentIdentities and cron agentId to data.json for v3 dashboard"
```

---

## Task 2: Create v3 HTML — Document skeleton and CSS

**Files:**
- Create: `/home/dawsmana/Documents/openclaw-dashboard/index-v3.html`

- [ ] **Step 1: Create the HTML skeleton with all CSS**

The file should contain the complete document structure. CSS first (in `<style>`), then HTML structure, then JS (in `<script>`).

**CSS design system:**
- Uses all CSS custom properties from `themes.json` (same as v2)
- Mobile-first: base styles for 375px width, `@media (min-width: 768px)` for tablet/desktop
- Card-based layout with `border-radius: 16px`, subtle shadows, `backdrop-filter: blur`
- Status dots: 10px circles, `--green`/`--yellow`/`--red`
- Agent cards: 2-column CSS grid on mobile, 3-4 columns on desktop
- Tap-to-expand via CSS `details/summary` elements (no JS needed for expand/collapse)
- Large text for key metrics: 20px+ for status labels
- Font: system font stack (same as v2)

**Layout sections (top to bottom):**
1. **Header bar** — Bot emoji + name + overall health pill (green/amber/red)
2. **System strip** — 4 compact pills: CPU, Disk, RAM, Gateway uptime
3. **Alert banner** — Only visible when issues exist, expandable list
4. **Agent grid** — 2-col cards, each card is a `<details>` element
5. **Today's schedule** — Chronological timeline of cron jobs for today
6. **Services** — Compact status list of core services + MCP servers
7. **Activity feed** — Last 10 events, scrollable

- [ ] **Step 2: Write the complete CSS**

Key CSS classes and their purpose:

```css
/* Root and reset — same approach as v2 */
/* .ops-dash — main container, max-width 600px on mobile, 960px desktop */
/* .health-pill — rounded pill showing overall status, color-coded */
/* .sys-strip — horizontal flex row of 4 small metric pills */
/* .alert-banner — amber/red background, only shown when alerts exist */
/* .agent-grid — CSS grid, 2 columns on mobile, 3-4 on desktop */
/* .agent-card — surface background card with emoji, name, status dot */
/* .agent-card[open] — expanded state showing model, crons, skills */
/* .schedule-timeline — vertical timeline with time + job name + status icon */
/* .service-list — compact list with status dots */
/* .activity-feed — scrollable list, max-height 300px */
```

- [ ] **Step 3: Write the HTML structure**

All sections use semantic HTML. Agent cards use `<details><summary>` for native expand/collapse. No unnecessary divs.

- [ ] **Step 4: Verify the file loads in browser (empty state)**

Open `http://100.87.79.17:8088/index-v3.html` — should show the skeleton with placeholder text.

- [ ] **Step 5: Commit**

```bash
cd /home/dawsmana/Documents/openclaw-dashboard
git add index-v3.html
git commit -m "feat: v3 operations dashboard skeleton with CSS design system"
```

---

## Task 3: JavaScript — Data fetching and rendering

**Files:**
- Modify: `/home/dawsmana/Documents/openclaw-dashboard/index-v3.html`

- [ ] **Step 1: Write the data fetching and auto-refresh logic**

```javascript
let DATA = null;
const REFRESH_MS = 60000;

async function loadData() {
  try {
    const res = await fetch('/api/refresh');
    DATA = await res.json();
    render(DATA);
  } catch (e) {
    console.error('Refresh failed:', e);
  }
}

// Initial load + auto-refresh
loadData();
setInterval(loadData, REFRESH_MS);
```

- [ ] **Step 2: Write the `render(data)` function skeleton**

```javascript
function render(d) {
  renderHeader(d);
  renderSystemStrip(d);
  renderAlerts(d);
  renderAgents(d);
  renderSchedule(d);
  renderServices(d);
  renderActivity(d);
}
```

- [ ] **Step 3: Implement `renderHeader(d)`**

Shows bot emoji + name + overall health pill. Health logic:
- RED if gateway offline OR any critical alert
- AMBER if any warning-level alert OR cron failures in last 24h
- GREEN otherwise

```javascript
function renderHeader(d) {
  const el = document.getElementById('header');
  const gw = d.gateway || {};
  const alerts = d.alerts || [];
  const hasError = gw.status !== 'online' || alerts.some(a => a.severity === 'high');
  const hasWarn = alerts.some(a => a.severity === 'medium');
  const status = hasError ? 'red' : hasWarn ? 'amber' : 'green';
  const label = hasError ? 'Needs attention' : hasWarn ? 'Minor issues' : 'All systems go';
  // ... set innerHTML
}
```

- [ ] **Step 4: Implement `renderSystemStrip(d)`**

4 compact pills showing CPU temp, disk %, load average, gateway uptime. Color-code based on thresholds:
- CPU: green <70, amber <85, red >=85
- Disk: green <70%, amber <85%, red >=85%
- Load: green <4, amber <8, red >=8

- [ ] **Step 5: Implement `renderAlerts(d)`**

Only visible when `d.alerts.length > 0`. Shows count badge and expandable list. Each alert shows icon + message.

- [ ] **Step 6: Commit**

```bash
git add index-v3.html
git commit -m "feat: v3 data fetching, header, system strip, and alerts"
```

---

## Task 4: JavaScript — Agent cards

**Files:**
- Modify: `/home/dawsmana/Documents/openclaw-dashboard/index-v3.html`

- [ ] **Step 1: Implement `renderAgents(d)`**

This is the core of v3. Each agent gets a card with:

**Collapsed state (visible at a glance):**
- Emoji + display name (from agentIdentities)
- One-line role (creature field, truncated)
- Activity dot: green if active in last 30min, dim grey if idle
- Last active time: "2m ago" / "3h ago" / "idle"

**Expanded state (tap to reveal):**
- Model name (simplified: "Sonnet 4.6" not "github-copilot/claude-sonnet-4.6")
- Tagline from IDENTITY.md
- Cron jobs belonging to this agent (from crons array filtered by agentId):
  - Each shows: status dot (green=ok, red=failed) + name + "last ran Xh ago"
- Skills list as small pills/tags

```javascript
function renderAgents(d) {
  const grid = document.getElementById('agent-grid');
  const identities = d.agentIdentities || {};
  const agents = (d.agentConfig?.agents || []).filter(a => !a.id.startsWith('bench-'));
  const crons = d.crons || [];
  const skills = d.skills || [];

  grid.innerHTML = agents.map(agent => {
    const id = agent.id;
    const identity = identities[id] || {};
    const agentCrons = crons.filter(c => c.agentId === id && c.enabled);
    const agentSkills = skills.filter(s => s.agent === id);
    const lastMs = identity.lastActivityMs || 0;
    const ago = lastMs ? timeAgo(lastMs) : 'idle';
    const isActive = lastMs && (Date.now() - lastMs < 30 * 60 * 1000);
    const emoji = identity.emoji || agent.role?.[0] || '?';
    const name = identity.name || agent.role || id;
    const creature = identity.creature || '';
    const model = simplifyModel(agent.model || '');

    return `<details class="agent-card">
      <summary>
        <span class="agent-emoji">${emoji}</span>
        <div class="agent-summary">
          <div class="agent-name">${name}</div>
          <div class="agent-role">${creature}</div>
        </div>
        <div class="agent-status">
          <span class="dot ${isActive ? 'dot-green' : 'dot-dim'}"></span>
          <span class="agent-ago">${ago}</span>
        </div>
      </summary>
      <div class="agent-detail">
        <div class="agent-model">Model: ${model}</div>
        ${identity.tagline ? `<div class="agent-tagline">${identity.tagline}</div>` : ''}
        ${agentCrons.length ? `
          <div class="agent-section-label">Scheduled Jobs</div>
          ${agentCrons.map(c => {
            const st = c.lastStatus === 'ok' ? 'green' : c.lastStatus === 'error' ? 'red' : 'dim';
            const cronAgo = c.lastRun ? timeAgo(new Date(c.lastRun.replace(' ', 'T') + ':00Z').getTime()) : 'never';
            return `<div class="agent-cron"><span class="dot dot-${st}"></span> ${c.name} <span class="muted">${cronAgo}</span></div>`;
          }).join('')}
        ` : ''}
        ${agentSkills.length ? `
          <div class="agent-section-label">Skills</div>
          <div class="skill-tags">${agentSkills.map(s => `<span class="skill-tag">${s.name}</span>`).join('')}</div>
        ` : ''}
      </div>
    </details>`;
  }).join('');
}
```

- [ ] **Step 2: Write helper functions**

```javascript
function timeAgo(ms) {
  const diff = Date.now() - ms;
  if (diff < 60000) return 'just now';
  if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
  if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
  return Math.floor(diff / 86400000) + 'd ago';
}

function simplifyModel(m) {
  // "github-copilot/claude-sonnet-4.6" -> "Sonnet 4.6"
  // "github-copilot/gpt-4.1" -> "GPT 4.1"
  // "kimi-coding/k2p5" -> "Kimi K2P5"
  return m.replace(/^[^/]+\//, '')
    .replace('claude-', '').replace('gpt-', 'GPT ')
    .replace('gemini-', 'Gemini ')
    .split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}
```

- [ ] **Step 3: Test agent cards render correctly**

Open `http://100.87.79.17:8088/index-v3.html` — verify:
- 11 agent cards visible (router excluded or shown)
- Each shows emoji, name, role, activity status
- Tapping a card expands to show model, cron jobs, skills
- Cron jobs show green/red status dots

- [ ] **Step 4: Commit**

```bash
git add index-v3.html
git commit -m "feat: v3 agent cards with expand/collapse, crons, skills"
```

---

## Task 5: JavaScript — Schedule timeline, services, activity feed

**Files:**
- Modify: `/home/dawsmana/Documents/openclaw-dashboard/index-v3.html`

- [ ] **Step 1: Implement `renderSchedule(d)`**

Shows today's cron jobs sorted chronologically. Each row: time + status icon + job name + agent name.

Status icons:
- `✓` green — ran successfully today
- `✗` red — failed today
- `◷` muted — upcoming (next run is today, hasn't run yet)
- `—` dim — not scheduled today

Only show jobs that ran today or are scheduled for today. Collapsed by default to top 8, "Show all (N jobs)" expander.

```javascript
function renderSchedule(d) {
  const el = document.getElementById('schedule');
  const now = new Date();
  const todayStr = now.toISOString().slice(0, 10);
  const crons = (d.crons || []).filter(c => c.enabled);
  const identities = d.agentIdentities || {};

  // Use lastRefresh date as "today" reference (London time, avoids BST/UTC mismatch)
  const refDate = (d.lastRefresh || '').slice(0, 10) || now.toISOString().slice(0, 10);
  const entries = crons.map(c => {
    const lastRan = c.lastRun || '';
    const ranToday = lastRan.startsWith(refDate);
    const nextToday = (c.nextRun || '').startsWith(refDate);
    const time = ranToday ? lastRan.slice(11, 16) : (nextToday ? c.nextRun.slice(11, 16) : null);
    if (!time) return null;
    const status = ranToday ? (c.lastStatus === 'ok' ? 'ok' : 'error') : 'upcoming';
    const agentName = identities[c.agentId]?.name || c.agentId || '';
    return { time, name: c.name, status, agentName, sortKey: time };
  }).filter(Boolean).sort((a, b) => a.sortKey.localeCompare(b.sortKey));

  // Render
}
```

- [ ] **Step 2: Implement `renderServices(d)`**

Compact list showing:
- Gateway: status + uptime
- MCP servers: "N/M passing" with overall dot
- Individual MCP server status if any failed

```javascript
function renderServices(d) {
  const el = document.getElementById('services');
  const gw = d.gateway || {};
  const mcps = d.mcpServers || [];
  const mcpOk = mcps.filter(m => m.status === 'ok').length;
  const mcpTotal = mcps.length;
  const mcpFailed = mcps.filter(m => m.status !== 'ok');
  // Render gateway status + MCP summary + any failed servers
}
```

- [ ] **Step 3: Implement `renderActivity(d)`**

Shows last 10 items from `activityFeed`. Each row: time + icon + message. Scrollable, max-height 300px.

- [ ] **Step 4: Add theme support**

Same approach as v2 — fetch `themes.json`, apply CSS variables, persist to localStorage:

```javascript
async function loadThemes() {
  const res = await fetch('/themes.json');
  const themes = await res.json();
  const saved = localStorage.getItem('ocDashTheme') || 'midnight';
  applyTheme(themes[saved] || themes.midnight);
}
function applyTheme(theme) {
  const r = document.documentElement;
  for (const [key, val] of Object.entries(theme.colors)) {
    r.style.setProperty('--' + key, val);
  }
}
```

- [ ] **Step 5: Add theme picker (small gear icon in header)**

Dropdown with 6 theme options. On select, apply + save to localStorage.

- [ ] **Step 6: Full integration test**

Open `http://100.87.79.17:8088/index-v3.html` on mobile viewport (375px) and verify:
- Header shows bot name + health status
- System strip shows 4 metrics with appropriate colors
- Alert banner appears only when alerts exist
- Agent cards: 2 columns, tap to expand, crons + skills visible
- Schedule: today's jobs in chronological order
- Services: gateway + MCP status
- Activity feed: last 10 events
- Theme picker works, survives page reload
- Auto-refresh updates every 60s

- [ ] **Step 7: Commit**

```bash
git add index-v3.html
git commit -m "feat: v3 schedule timeline, services panel, activity feed, theme support"
```

---

## Task 6: Polish — Animations, empty states, edge cases

**Files:**
- Modify: `/home/dawsmana/Documents/openclaw-dashboard/index-v3.html`

- [ ] **Step 1: Add smooth transitions**

- Card expand/collapse: CSS `transition` on `max-height` or use `<details>` native with `transition` on inner content
- Status dot pulse animation for active agents
- Fade-in on data load

```css
.dot-green { animation: pulse 2s infinite; }
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
details[open] .agent-detail {
  animation: slideDown 0.2s ease-out;
}
@keyframes slideDown {
  from { opacity: 0; transform: translateY(-8px); }
  to { opacity: 1; transform: translateY(0); }
}
```

- [ ] **Step 2: Handle empty states**

- No agents: "No agents configured"
- No cron jobs for an agent: Don't show the "Scheduled Jobs" section
- No skills: Don't show the "Skills" section
- Gateway offline: Header shows red pill, system strip shows "Offline"
- No alerts: Alert banner hidden entirely (not "No alerts")
- No activity: "No recent activity"

- [ ] **Step 3: Add refresh indicator**

Small spinner or pulse on the header during refresh. "Updated Xs ago" text that counts up.

- [ ] **Step 4: Add pull-to-refresh hint on mobile**

```javascript
// Simple: just refresh on pull gesture
let touchStartY = 0;
document.addEventListener('touchstart', e => { touchStartY = e.touches[0].clientY; });
document.addEventListener('touchend', e => {
  if (window.scrollY === 0 && e.changedTouches[0].clientY - touchStartY > 100) {
    loadData();
  }
});
```

- [ ] **Step 5: Final visual review on mobile + desktop**

Verify at 375px, 768px, 1024px, 1440px widths. Ensure no horizontal scroll, text is readable, cards don't overflow.

- [ ] **Step 6: Commit**

```bash
cd /home/dawsmana/Documents/openclaw-dashboard
git add index-v3.html
git commit -m "feat: v3 polish — animations, empty states, pull-to-refresh"
```

---

## Task 7: Wire up server routing

**Files:**
- Modify: `/home/dawsmana/Documents/openclaw-dashboard/server.py` (optional, only if we want v3 as default)

- [ ] **Step 1: Verify v3 is accessible at `/index-v3.html`**

The existing `SimpleHTTPRequestHandler` serves any file from the repo directory. No server changes needed to access v3 — it's already available at `http://100.87.79.17:8088/index-v3.html`.

- [ ] **Step 2: Test end-to-end**

1. `systemctl --user restart openclaw-dashboard`
2. Open `http://100.87.79.17:8088/index-v3.html` on phone
3. Verify all panels render with live data
4. Verify auto-refresh works (wait 60s, see data update)
5. Verify theme switching works
6. Verify agent card expand/collapse works
7. Verify schedule shows today's cron jobs with correct status

- [ ] **Step 3: Commit final state**

```bash
cd /home/dawsmana/Documents/openclaw-dashboard
git add -A
git commit -m "feat: v3 operations dashboard complete"
```
