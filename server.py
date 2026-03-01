#!/usr/bin/env python3
"""OpenClaw Dashboard Server — static files + on-demand refresh."""

import argparse
import functools
import http.server
import json
import os
import re
import socket
import subprocess
import threading
import time
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

VERSION = "2.4.0"
PORT = 8088
BIND = "127.0.0.1"
DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DIR, "config.json")
REFRESH_SCRIPT = os.path.join(DIR, "refresh.sh")
DATA_FILE = os.path.join(DIR, "data.json")
REFRESH_TIMEOUT = 15

# NocoDB config for kanban panel
NOCODB_URL = "http://127.0.0.1:8080"
NOCODB_TABLE_ID = "mjdq7yahdxeeqnd"
NOCODB_TOKEN_FILE = os.path.expanduser("~/containers/nocodb/nocodb-mcp.env")


def get_nocodb_token():
    """Read NocoDB API token from env file."""
    try:
        with open(NOCODB_TOKEN_FILE, "r") as f:
            for line in f:
                if line.startswith("NOCODB_API_TOKEN="):
                    return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return None


def fetch_kanban_data():
    """Fetch tasks from NocoDB and group by status."""
    token = get_nocodb_token()
    if not token:
        return {"error": "NocoDB token not found"}

    url = f"{NOCODB_URL}/api/v2/tables/{NOCODB_TABLE_ID}/records?limit=200&sort=-Created%20At"
    req = urllib.request.Request(url)
    req.add_header("xc-token", token)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError) as e:
        return {"error": f"NocoDB request failed: {e}"}

    records = data.get("list", [])
    columns = {
        "backlog": [], "queued": [], "in_progress": [],
        "review": [], "done": [], "failed": [], "escalated": []
    }

    completed_today = 0
    total_minutes = 0
    completed_count = 0
    agent_counts = {}

    for rec in records:
        status = (rec.get("Status") or "backlog").lower().replace(" ", "_")
        if status not in columns:
            status = "backlog"

        card = {
            "id": rec.get("Id"),
            "title": rec.get("Title", ""),
            "status": status,
            "type": rec.get("Type", ""),
            "priority": rec.get("Priority", "normal"),
            "agent": rec.get("Agent", ""),
            "skill": rec.get("Skill", ""),
            "tools": rec.get("Tools", ""),
            "source": rec.get("Source", ""),
            "acceptance_criteria": rec.get("Acceptance Criteria", ""),
            "result": rec.get("Result", ""),
            "attempts": rec.get("Attempts", 0),
            "max_attempts": rec.get("Max Attempts", 3),
            "created_at": rec.get("Created At", ""),
            "started_at": rec.get("Started At", ""),
            "completed_at": rec.get("Completed At", ""),
            "estimated_minutes": rec.get("Estimated Minutes"),
            "actual_minutes": rec.get("Actual Minutes"),
            "channel": rec.get("Channel", ""),
            "session_key": rec.get("Session Key", ""),
            "notes": rec.get("Notes", ""),
        }

        if status == "done":
            completed_at = rec.get("Completed At", "")
            if completed_at:
                try:
                    dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                    if age_hours > 24:
                        continue
                    completed_today += 1
                except (ValueError, TypeError):
                    pass
            actual = rec.get("Actual Minutes")
            if actual:
                total_minutes += actual
                completed_count += 1

        if status in ("queued", "in_progress", "review"):
            agent = rec.get("Agent", "unknown")
            agent_counts[agent] = agent_counts.get(agent, 0) + 1

        columns[status].append(card)

    active = len(columns["queued"]) + len(columns["in_progress"]) + len(columns["review"])
    avg_minutes = round(total_minutes / completed_count) if completed_count else 0

    return {
        "columns": columns,
        "stats": {
            "active": active,
            "completed_today": completed_today,
            "avg_completion_minutes": avg_minutes,
            "agent_counts": agent_counts,
        }
    }


_last_refresh = 0
_refresh_lock = threading.Lock()
_debounce_sec = 30
_ai_cfg = {}
_gateway_token = ""


OPENCLAW_PATH = os.path.expanduser("~/.openclaw")


def _load_agent_default_models():
    """Read agent default models from openclaw.json dynamically."""
    try:
        with open(os.path.join(OPENCLAW_PATH, "openclaw.json")) as f:
            cfg = json.load(f)
        primary = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "unknown")
        defaults = {}
        agents = cfg.get("agents", {})
        for name, val in agents.items():
            if name == "defaults" or not isinstance(val, dict):
                continue
            agent_primary = val.get("model", {}).get("primary", primary)
            defaults[name] = agent_primary
        # Ensure common agents have entries
        for a in ("main", "work", "group"):
            if a not in defaults:
                defaults[a] = primary
        return defaults
    except Exception:
        return {"main": "unknown", "work": "unknown", "group": "unknown"}


def _ttl_hash(ttl_seconds=300):
    """Return a hash that changes every ttl_seconds (default 5 min)."""
    return int(time.time() // ttl_seconds)


@functools.lru_cache(maxsize=512)
def _get_session_model_cached(session_key, jsonl_path, _ttl):
    """Cached model lookup from JSONL file. _ttl param drives cache invalidation."""
    try:
        with open(jsonl_path, "r") as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "model_change":
                        provider = obj.get("provider", "")
                        model_id = obj.get("modelId", "")
                        if provider and model_id:
                            return f"{provider}/{model_id}"
                except (json.JSONDecodeError, ValueError):
                    continue
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def get_session_model(session_key, session_file=None):
    """Get the model for a session by reading its JSONL file.

    Reads first 10 lines looking for a model_change event.
    Uses LRU cache with 5-minute TTL for performance.
    Falls back to agent config defaults if JSONL is missing.
    """
    # Determine JSONL path from session_file or session_key
    jsonl_path = None
    if session_file and os.path.exists(session_file):
        jsonl_path = session_file
    else:
        # Try to find it from sessions.json
        parts = (session_key or "").split(":")
        agent_name = parts[1] if len(parts) >= 2 else "main"
        sessions_json = os.path.join(
            OPENCLAW_PATH, "agents", agent_name, "sessions", "sessions.json"
        )
        try:
            with open(sessions_json, "r") as f:
                store = json.load(f)
            session_data = store.get(session_key, {})
            sid = session_data.get("sessionId", "")
            if sid:
                candidate = os.path.join(
                    OPENCLAW_PATH, "agents", agent_name, "sessions", f"{sid}.jsonl"
                )
                if os.path.exists(candidate):
                    jsonl_path = candidate
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            pass

    if jsonl_path:
        result = _get_session_model_cached(session_key, jsonl_path, _ttl_hash())
        if result:
            return result

    # Fallback to agent defaults
    parts = (session_key or "").split(":")
    agent_name = parts[1] if len(parts) >= 2 else "main"
    return _load_agent_default_models().get(agent_name, "unknown")


def load_config():
    """Load config.json, return empty dict on failure."""
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def read_dotenv(path):
    """Read a KEY=VALUE .env file, return dict. Ignores comments and blanks."""
    result = {}
    try:
        expanded = os.path.expanduser(path)
        with open(expanded, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except (FileNotFoundError, PermissionError):
        pass
    return result


def build_dashboard_prompt(data):
    """Build a compressed system prompt from data.json for the AI assistant."""
    gw = data.get("gateway") or {}
    ac = data.get("agentConfig") or {}

    lines = [
        "You are an AI assistant embedded in the OpenClaw Dashboard.",
        "Answer questions concisely. Use plain text, no markdown.",
        f"Data as of: {data.get('lastRefresh', 'unknown')}",
        "",
        "=== GATEWAY ===",
        f"Status: {gw.get('status', '?')} | PID: {gw.get('pid', '?')} | "
        f"Uptime: {gw.get('uptime', '?')} | Memory: {gw.get('memory', '?')}",
    ]

    # Provider status
    lines += ["", "=== PROVIDERS ==="]
    ps = data.get("providerStatus") or {}
    pc = data.get("providerCalls") or {}
    for pid, info in ps.items():
        calls_today = pc.get("today", {}).get(pid, 0)
        calls_7d = pc.get("7d", {}).get(pid, 0)
        status = info.get("status", "?")
        extra = ""
        if info.get("cooldownRemainingMs"):
            mins = info["cooldownRemainingMs"] // 60000
            extra = f" (cooldown {mins}m)"
        elif info.get("tokenRemainingDays") is not None:
            extra = f" (token {info['tokenRemainingDays']}d left)"
        lines.append(
            f"  {pid}: {status}{extra} | "
            f"{calls_today} calls today, {calls_7d} 7d | "
            f"{info.get('errorCount', 0)} errors, {info.get('rateLimitCount', 0)} rate limits"
        )

    # System vitals
    sv = data.get("systemVitals") or {}
    lines += [
        "", "=== SYSTEM ===",
        f"CPU: {sv.get('cpuTemp', '?')}°C | "
        f"Disk: {sv.get('diskUsedPct', '?')}% ({sv.get('diskFreeGb', '?')}GB free) | "
        f"Load: {sv.get('loadAvg', '?')}"
    ]

    sess = data.get("sessions") or []
    lines += [
        "",
        f"=== SESSIONS ({data.get('sessionCount', len(sess))} total, showing top 3) ===",
    ]
    for s in sess[:3]:
        lines.append(
            f"  {s.get('name', '?')} | {s.get('model', '?')} | "
            f"{s.get('type', '?')} | context: {s.get('contextPct', 0)}%"
        )

    crons = data.get("crons") or []
    failed = [c for c in crons if c.get("lastStatus") == "error"]
    lines += [
        "",
        f"=== CRON JOBS ({len(crons)} total, {len(failed)} failed) ===",
    ]
    for c in crons[:5]:
        status = c.get("lastStatus", "?")
        err = f" ERROR: {c.get('lastError', '')}" if status == "error" else ""
        lines.append(f"  {c.get('name', '?')} | {c.get('schedule', '?')} | {status}{err}")

    alerts = data.get("alerts") or []
    lines += ["", "=== ALERTS ==="]
    if alerts:
        for a in alerts:
            lines.append(f"  [{a.get('severity', '?').upper()}] {a.get('message', '?')}")
    else:
        lines.append("  None")

    lines += [
        "",
        "=== CONFIGURATION ===",
        f"Primary model: {ac.get('primaryModel', '?')}",
        f"Fallbacks: {', '.join(ac.get('fallbacks', [])) or 'none'}",
    ]

    # Task board context from NocoDB
    try:
        kb = fetch_kanban_data()
        if "error" not in kb:
            st = kb.get("stats", {})
            cols = kb.get("columns", {})
            total = sum(len(v) for v in cols.values())
            lines += ["", f"=== TASK BOARD ({total} tasks) ==="]
            lines.append(
                f"Active: {st.get('active', 0)} | "
                f"Completed today: {st.get('completed_today', 0)} | "
                f"Avg completion: {st.get('avg_completion_minutes', 0)}min"
            )
            agents = st.get("agent_counts", {})
            if agents:
                lines.append("By agent: " + ", ".join(
                    f"{a}({n})" for a, n in agents.items()
                ))
            for status in ("in_progress", "queued", "review", "failed", "escalated"):
                for card in cols.get(status, []):
                    lines.append(
                        f"  [{status}] {card.get('title', '?')} "
                        f"(agent: {card.get('agent', '?')}, "
                        f"priority: {card.get('priority', '?')})"
                    )
    except Exception:
        pass

    return "\n".join(lines)


def call_gateway(system, history, question, port, token, model):
    """Call the OpenClaw gateway's OpenAI-compatible chat completions endpoint.

    Returns {"answer": "..."} on success, {"error": "..."} on failure.
    """
    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 512,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"http://localhost:{port}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
            content = (
                body.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
            )
            return {"answer": content or "(empty response)"}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"error": f"Gateway HTTP {e.code}: {body[:200]}"}
    except urllib.error.URLError as e:
        return {"error": f"Gateway unreachable: {e.reason}"}
    except socket.timeout:
        return {"error": "Gateway timed out — model took too long to respond"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    ALLOWED_CONTAINERS = [
        "rocketchat-app", "rocketchat-mongo", "nocodb-app", "nocodb-pg",
        "adguard-home", "fantasy-pl-mcp", "google-analytics-mcp",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def end_headers(self):
        # Prevent browser caching of HTML/JS files
        if hasattr(self, 'path') and (self.path.endswith('.html') or self.path == '/' or self.path.endswith('.js')):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        if self.path == "/api/refresh" or self.path.startswith("/api/refresh?"):
            self.handle_refresh()
        elif self.path == "/api/kanban" or self.path.startswith("/api/kanban?"):
            self.handle_kanban()
        elif self.path.startswith("/api/kanban/stats"):
            self.handle_kanban_stats()
        elif self.path.startswith("/api/kanban/task/"):
            self.handle_kanban_task()
        elif self.path == "/api/containers":
            self._send_json(200, {"containers": DashboardHandler.ALLOWED_CONTAINERS})
        elif self.path == "/api/cron-jobs":
            self.handle_cron_list()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/chat":
            self.handle_chat()
        elif self.path == "/api/action/restart-gateway":
            self.handle_action_restart_gateway()
        elif self.path == "/api/action/restart-container":
            self.handle_action_restart_container()
        elif self.path == "/api/action/run-cron":
            self.handle_action_run_cron()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_refresh(self):
        run_refresh()

        try:
            with open(DATA_FILE, "r") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            origin = self.headers.get("Origin", "")
            if origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:"):
                self.send_header("Access-Control-Allow-Origin", origin)
            else:
                self.send_header("Access-Control-Allow-Origin", "http://localhost:8080")
            self.end_headers()
            self.wfile.write(data.encode())
        except FileNotFoundError:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "data.json not found"}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_chat(self):
        if not _ai_cfg.get("enabled", True):
            self._send_json(503, {"error": "AI chat is disabled in config.json"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        question = body.get("question", "").strip()
        if not question:
            self._send_json(400, {"error": "question is required and must be non-empty"})
            return

        history = body.get("history", [])
        if not isinstance(history, list):
            history = []
        max_hist = int(_ai_cfg.get("maxHistory", 6))
        history = history[-max_hist:]

        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        system_prompt = build_dashboard_prompt(data)
        result = call_gateway(
            system=system_prompt,
            history=history,
            question=question,
            port=int(_ai_cfg.get("gatewayPort", 18789)),
            token=_gateway_token,
            model=_ai_cfg.get("model", "kimi-coding/k2p5"),
        )
        self._send_json(200, result)

    def _send_json(self, status, data):
        """Send a JSON response with CORS headers."""
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        origin = self.headers.get("Origin", "")
        if origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:") or origin.startswith("http://100.87.79.17:"):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "http://localhost:8088")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_kanban(self):
        try:
            data = fetch_kanban_data()
            if "error" in data:
                self._send_json(503, data)
            else:
                self._send_json(200, data)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def handle_kanban_stats(self):
        try:
            data = fetch_kanban_data()
            if "error" in data:
                self._send_json(503, data)
            else:
                self._send_json(200, data.get("stats", {}))
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def handle_kanban_task(self):
        try:
            task_id = self.path.split("/api/kanban/task/")[1].split("?")[0]
        except (IndexError, ValueError):
            self._send_json(400, {"error": "Invalid task ID"})
            return

        if not re.match(r'^[a-zA-Z0-9_-]+$', task_id):
            self._send_json(400, {"error": "Invalid task ID"})
            return

        token = get_nocodb_token()
        if not token:
            self._send_json(503, {"error": "NocoDB token not found"})
            return

        url = f"{NOCODB_URL}/api/v2/tables/{NOCODB_TABLE_ID}/records/{task_id}"
        req = urllib.request.Request(url)
        req.add_header("xc-token", token)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                record = json.loads(resp.read().decode())
            self._send_json(200, record)
        except urllib.error.HTTPError as e:
            self._send_json(e.code, {"error": f"NocoDB: {e.code}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def handle_action_restart_gateway(self):
        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", "openclaw-gateway"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                self._send_json(200, {"ok": True, "message": "Gateway restarted"})
            else:
                self._send_json(500, {"ok": False, "error": result.stderr.strip()})
        except subprocess.TimeoutExpired:
            self._send_json(504, {"ok": False, "error": "Restart timed out"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def handle_action_restart_container(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "Invalid JSON"})
            return
        container = body.get("container", "")
        if container not in self.ALLOWED_CONTAINERS:
            self._send_json(400, {"error": f"Container '{container}' not in allowlist"})
            return
        try:
            result = subprocess.run(
                ["podman", "restart", container],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                self._send_json(200, {"ok": True, "message": f"Restarted {container}"})
            else:
                self._send_json(500, {"ok": False, "error": result.stderr.strip()})
        except subprocess.TimeoutExpired:
            self._send_json(504, {"ok": False, "error": "Restart timed out"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def handle_action_run_cron(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "Invalid JSON"})
            return
        job_id = body.get("jobId", "")
        if not re.match(r'^[a-f0-9]{8}$', job_id):
            self._send_json(400, {"error": "Invalid job ID format"})
            return
        try:
            result = subprocess.run(
                ["openclaw", "cron", "run", "--timeout", "120000", job_id],
                capture_output=True, text=True, timeout=130,
            )
            if result.returncode == 0:
                self._send_json(200, {"ok": True, "message": f"Triggered cron {job_id}"})
            else:
                self._send_json(500, {"ok": False, "error": result.stderr.strip()[:200]})
        except subprocess.TimeoutExpired:
            self._send_json(504, {"ok": False, "error": "Cron execution timed out"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def handle_cron_list(self):
        cron_path = os.path.join(OPENCLAW_PATH, "cron", "jobs.json")
        try:
            with open(cron_path) as f:
                jobs = json.load(f).get("jobs", [])
            result = [
                {"id": j.get("id", "")[:8], "name": j.get("name", ""), "enabled": j.get("enabled", True)}
                for j in jobs if j.get("enabled", True)
            ]
            self._send_json(200, {"jobs": result})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def log_message(self, format, *args):
        # Quiet logging — only log errors and refreshes
        msg = format % args
        if "/api/refresh" in msg or "/api/chat" in msg or "error" in msg.lower():
            print(f"[dashboard] {msg}")


def resolve_config_value(key, cli_val, env_var, config_path, default):
    """Resolve config with priority: CLI flag > env var > config.json > default."""
    if cli_val is not None:
        return cli_val
    env_val = os.environ.get(env_var)
    if env_val is not None:
        return env_val
    cfg = load_config()
    parts = config_path.split(".")
    val = cfg
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        else:
            val = None
            break
    if val is not None:
        return val
    return default


def run_refresh():
    """Run refresh.sh with debounce and timeout."""
    global _last_refresh
    now = time.time()

    with _refresh_lock:
        if now - _last_refresh < _debounce_sec:
            return True  # debounced, serve cached

        try:
            subprocess.run(
                ["bash", REFRESH_SCRIPT],
                timeout=REFRESH_TIMEOUT,
                cwd=DIR,
                capture_output=True,
            )
            _last_refresh = time.time()
            return True
        except subprocess.TimeoutExpired:
            print(f"[dashboard] refresh.sh timed out after {REFRESH_TIMEOUT}s")
            return False
        except Exception as e:
            print(f"[dashboard] refresh.sh failed: {e}")
            return False


def main():
    cfg = load_config()
    server_cfg = cfg.get("server", {})
    refresh_cfg = cfg.get("refresh", {})

    cfg_bind = server_cfg.get("host", BIND)
    cfg_port = server_cfg.get("port", PORT)
    global _debounce_sec, _ai_cfg, _gateway_token
    _debounce_sec = refresh_cfg.get("intervalSeconds", _debounce_sec)

    # Load AI config and gateway token
    _ai_cfg = cfg.get("ai", {})
    dotenv_path = _ai_cfg.get("dotenvPath", "~/.openclaw/.env")
    env_vars = read_dotenv(dotenv_path)
    _gateway_token = env_vars.get("OPENCLAW_GATEWAY_TOKEN", "")
    if _ai_cfg.get("enabled", True) and not _gateway_token:
        print("[dashboard] WARNING: ai.enabled=true but OPENCLAW_GATEWAY_TOKEN not found in dotenv")

    env_bind = os.environ.get("DASHBOARD_BIND", cfg_bind)
    env_port = int(os.environ.get("DASHBOARD_PORT", cfg_port))

    parser = argparse.ArgumentParser(
        description="OpenClaw Dashboard Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""priority: CLI flags > env vars > config.json > defaults

examples:
  %(prog)s                          # localhost:8080 (default)
  %(prog)s --bind 0.0.0.0           # LAN access on port 8080
  %(prog)s -b 0.0.0.0 -p 9090      # LAN access on custom port
  DASHBOARD_BIND=0.0.0.0 %(prog)s   # env var override""",
    )
    parser.add_argument(
        "--bind", "-b",
        default=env_bind,
        help=f"Bind address (default: {env_bind}, use 0.0.0.0 for LAN)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=env_port,
        help=f"Listen port (default: {env_port})",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    args = parser.parse_args()

    server = http.server.HTTPServer((args.bind, args.port), DashboardHandler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    print(f"[dashboard] v{VERSION}")
    print(f"[dashboard] Serving on http://{args.bind}:{args.port}/")
    print(f"[dashboard] Refresh endpoint: /api/refresh (debounce: {_debounce_sec}s)")
    if _ai_cfg.get("enabled", True):
        print(f"[dashboard] AI chat: /api/chat (gateway: localhost:{_ai_cfg.get('gatewayPort', 18789)}, model: {_ai_cfg.get('model', '?')})")
    if args.bind == "0.0.0.0":
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            print(f"[dashboard] LAN access: http://{local_ip}:{args.port}/")
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
