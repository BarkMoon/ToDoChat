#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ToDoChat — local web prototype (Pattern A: wraps the headless claude CLI).

Python standard library only. Spawns the `claude` CLI in non-interactive (-p) mode
using the Pro subscription auth, so there is no per-token API billing.

The "working folder" (the app being developed) is switchable at runtime; the list
of folders is persisted to projects.json next to this app.
"""
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- paths / config ---------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8765
APP_DIR = Path(__file__).resolve().parent
APP_HOME = APP_DIR.parent                 # ToDoChat install dir (default project)
CONFIG_FILE = APP_HOME / "projects.json"  # persisted project list (gitignored)

# The headless CLI is installed at ~/.local/bin and is NOT on PATH -> full path.
_NATIVE = Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "claude.exe"
CLI = str(_NATIVE) if _NATIVE.exists() else "claude"

PERSONA = (
    "あなたは「ToDoChat」という個人開発タスクアシスタントです。"
    "ユーザーが隙間時間に即座に開発へ取り掛かれるよう支援します。"
    "常に日本語で、要点を先に、簡潔に答えます。"
    "先回りして次の具体的なアクションを1つ提案してください。"
)
# Two modes, chosen per-message from the UI (defaults to advisory, the safer one):
#   advisory - read-only exploration, no file/shell access.
#   edit     - file editing (Edit/Write) confined to the working folder.
#
# IMPORTANT safety note: in headless (-p) mode there is NO built-in classifier or
# path confinement. A tool listed in --allowedTools runs unconditionally and can
# write ANYWHERE on disk (verified: an allow-listed Write reaches absolute paths
# outside the folder). --permission-mode auto does not gate here either -- it just
# denies non-allow-listed tools. So the real boundary is a PreToolUse hook
# (app/edit_guard.py) that denies shell execution and any write that escapes the
# working folder. Bash is intentionally NOT granted in v1 (safe shell allow-listing
# is a separate, harder problem -- see TASKS.md).
ADVISORY_TOOLS = ["Read", "Glob", "Grep"]
EDIT_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write"]   # no Bash; hook confines writes
GUARD_HOOK = APP_DIR / "edit_guard.py"
TIMEOUT_SEC = 240


def guard_settings_arg():
    """A --settings JSON string registering the edit-mode PreToolUse guard hook.
    Passed inline (the CLI accepts a JSON string, not just a file path)."""
    py = sys.executable.replace("\\", "/")
    guard = str(GUARD_HOOK).replace("\\", "/")
    command = '"%s" "%s"' % (py, guard)
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": command}]},
    ]}})

# Models selectable per-message from the UI. Keys are what the client sends;
# values are the --model alias the CLI accepts ("opus"/"sonnet"/"haiku").
MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}
DEFAULT_MODEL = "sonnet"


def norm(p):
    """Normalized key for path comparison (case-insensitive on Windows)."""
    return os.path.normcase(os.path.normpath(str(p)))


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config():
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if cfg.get("projects") and cfg.get("current"):
                return cfg
        except (ValueError, OSError):
            pass
    default = {
        "current": str(APP_HOME),
        "projects": [{"path": str(APP_HOME), "name": APP_HOME.name}],
    }
    save_config(default)
    return default


CONFIG = load_config()
SESSIONS = {}   # norm(project_path) -> claude session_id (in-memory only)


# --- project management -----------------------------------------------------
def list_projects():
    return {"ok": True, "current": CONFIG["current"], "projects": CONFIG["projects"]}


def add_project(path):
    p = os.path.abspath(os.path.expanduser((path or "").strip().strip('"')))
    if not p or not os.path.isdir(p):
        return {"ok": False, "error": f"フォルダが見つかりません: {p or '(空)'}"}
    if not any(norm(x["path"]) == norm(p) for x in CONFIG["projects"]):
        CONFIG["projects"].append({"path": p, "name": os.path.basename(p) or p})
        save_config(CONFIG)
    return {"ok": True, "current": CONFIG["current"], "projects": CONFIG["projects"], "added": p}


def switch_project(path):
    match = next((x for x in CONFIG["projects"] if norm(x["path"]) == norm(path or "")), None)
    if not match:
        return {"ok": False, "error": "一覧にないフォルダです。"}
    CONFIG["current"] = match["path"]
    save_config(CONFIG)
    return {"ok": True, "current": CONFIG["current"], "projects": CONFIG["projects"]}


def remove_project(path):
    key = norm(path or "")
    CONFIG["projects"] = [x for x in CONFIG["projects"] if norm(x["path"]) != key]
    if not CONFIG["projects"]:
        CONFIG["projects"] = [{"path": str(APP_HOME), "name": APP_HOME.name}]
    if norm(CONFIG["current"]) == key:
        CONFIG["current"] = CONFIG["projects"][0]["path"]
    SESSIONS.pop(key, None)
    save_config(CONFIG)
    return {"ok": True, "current": CONFIG["current"], "projects": CONFIG["projects"]}


def browse_folder():
    """Pop up a native Windows folder picker on the server machine and return
    the chosen absolute path. Only meaningful for a local single-user app —
    the dialog appears on whoever's screen is running the server process."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return {"ok": False, "error": "この環境ではフォルダ選択ダイアログを表示できません。パスを直接入力してください。"}

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(title="ToDoChat: 開発対象フォルダを選択", parent=root)
    finally:
        root.destroy()

    if not path:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": os.path.normpath(path)}


# --- claude CLI wrapper -----------------------------------------------------
def extract_usage(data):
    u = data.get("usage") or {}
    inp = u.get("input_tokens", 0) or 0
    cr = u.get("cache_read_input_tokens", 0) or 0
    cc = u.get("cache_creation_input_tokens", 0) or 0
    out = u.get("output_tokens", 0) or 0
    ctx = 0
    for v in (data.get("modelUsage") or {}).values():
        ctx = max(ctx, v.get("contextWindow", 0) or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cc,
        "prompt_tokens": inp + cr + cc,      # size of this turn's prompt (context used)
        "context_window": ctx or 200000,
        "cost_usd": data.get("total_cost_usd", 0) or 0,
        "permission_denials": data.get("permission_denials") or [],
    }


def run_claude_stream(prompt, resume=True, model=None, mode=None):
    """Generator: yields {"type":"delta","text":...} chunks as the reply is
    generated, then exactly one {"type":"final", ok:..., reply/error, usage}
    event with the same shape the UI previously got from a single JSON call."""
    proj = CONFIG["current"]
    if not os.path.isdir(proj):
        yield {"type": "final", "ok": False, "error": f"作業フォルダが存在しません: {proj}"}
        return

    model_alias = MODELS.get(model, MODELS[DEFAULT_MODEL])
    edit_mode = mode == "edit"
    cmd = [
        CLI, "-p", prompt,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--append-system-prompt", PERSONA,
        "--allowedTools", *(EDIT_TOOLS if edit_mode else ADVISORY_TOOLS),
        "--model", model_alias,
    ]
    env = None
    if edit_mode:
        # The guard hook is the enforcement boundary; TODOCHAT_PROJECT_ROOT tells
        # it which folder writes must stay inside (matches this spawn's cwd).
        cmd += ["--settings", guard_settings_arg()]
        env = dict(os.environ, TODOCHAT_PROJECT_ROOT=proj)
    sid = SESSIONS.get(norm(proj)) if resume else None
    if sid:
        cmd += ["--resume", sid]

    try:
        # stderr is merged into stdout: reading only one pipe avoids the
        # classic subprocess deadlock (child blocks on a full stderr buffer
        # while we're only draining stdout). Stray non-JSON lines are just
        # skipped below.
        proc = subprocess.Popen(
            cmd, cwd=proj, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        yield {"type": "final", "ok": False, "error": f"claude CLI が見つかりません: {CLI}"}
        return

    timed_out = {"hit": False}

    def _kill_on_timeout():
        timed_out["hit"] = True
        proc.kill()

    timer = threading.Timer(TIMEOUT_SEC, _kill_on_timeout)
    timer.start()
    result_data = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "stream_event":
                ev = d.get("event") or {}
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield {"type": "delta", "text": delta["text"]}
            elif t == "result":
                result_data = d
    finally:
        timer.cancel()
        proc.wait()

    if timed_out["hit"]:
        yield {"type": "final", "ok": False, "error": "応答がタイムアウトしました。もう一度お試しください。"}
        return
    if result_data is None:
        yield {"type": "final", "ok": False, "error": "CLIから応答がありませんでした。"}
        return
    if result_data.get("is_error"):
        yield {"type": "final", "ok": False, "error": result_data.get("result") or "エラーが発生しました。"}
        return

    new_sid = result_data.get("session_id")
    if new_sid:
        SESSIONS[norm(proj)] = new_sid
    yield {"type": "final", "ok": True, "reply": result_data.get("result", ""), "usage": extract_usage(result_data)}


def read_tasks(d):
    try:
        return (Path(d) / "TASKS.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return "(このフォルダに TASKS.md はありません)"


def init_greeting_stream(model=None, mode=None):
    proj = CONFIG["current"]
    SESSIONS.pop(norm(proj), None)   # start a fresh conversation for this folder
    if not os.path.isdir(proj):
        yield {"type": "final", "ok": False, "error": f"作業フォルダが存在しません: {proj}"}
        return
    prompt = (
        f"作業対象フォルダ: {proj}\n\n"
        "以下はこのフォルダの TASKS.md の内容です:\n\n"
        f"{read_tasks(proj)}\n\n"
        "起動時の第一声として、次の3点を短くまとめてください:\n"
        "(1) 現状のタスク状況を1〜2文で要約\n"
        "(2) 次に取り掛かるべきタスクを1つ提案\n"
        "(3) 他に優先すべきことがないかユーザーへ一言問いかけ\n"
        "TASKS.md が無い場合は、フォルダ内のコードやREADMEなどから状況を推測して要約してください。"
    )
    yield from run_claude_stream(prompt, resume=False, model=model, mode=mode)


# --- HTTP handler -----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def _stream_ndjson(self, events):
        """Stream a generator of dicts to the client as newline-delimited JSON,
        flushing after each one so the browser sees deltas as they're produced."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for ev in events:
                self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # client navigated away / closed the tab mid-stream

    def do_GET(self):
        if self.path == "/api/projects":
            self._send_json(list_projects())
            return
        if self.path in ("/", "/index.html"):
            try:
                html = (APP_DIR / "index.html").read_bytes()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/init":
            body = self._read_body()
            self._stream_ndjson(init_greeting_stream(model=body.get("model"), mode=body.get("mode")))
        elif self.path == "/api/chat":
            body = self._read_body()
            msg = (body.get("message") or "").strip()
            if not msg:
                self._send_json({"ok": False, "error": "メッセージが空です。"})
                return
            self._stream_ndjson(run_claude_stream(msg, resume=True, model=body.get("model"), mode=body.get("mode")))
        elif self.path == "/api/projects/add":
            self._send_json(add_project(self._read_body().get("path")))
        elif self.path == "/api/projects/switch":
            self._send_json(switch_project(self._read_body().get("path")))
        elif self.path == "/api/projects/remove":
            self._send_json(remove_project(self._read_body().get("path")))
        elif self.path == "/api/projects/browse":
            self._send_json(browse_folder())
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass  # keep the console quiet


# --add-dir candidates for a Chromium browser's --app= "site as a window" mode
# (no tabs/address bar), so ToDoChat can't get lost among other browser tabs.
_APP_BROWSER_CANDIDATES = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


# Dedicated Chromium profile for ToDoChat's app window. --app mode only opens
# a separate tab-less window when it's a distinct profile/instance -- reusing
# the user's normal Edge/Chrome profile just opens a new tab in whatever
# browser window is already running (tested: verified this actually happens).
APP_PROFILE_DIR = APP_HOME / ".browser-profile"


def find_app_browser():
    for c in _APP_BROWSER_CANDIDATES:
        p = os.path.expandvars(c)
        if os.path.isfile(p):
            return p
    return None


def open_app_window(url):
    """Open ToDoChat as its own window (no tabs/URL bar) via Edge/Chrome --app
    mode in a dedicated profile, so it can't get buried among the user's other
    browser tabs. Falls back to a normal tab if no Chromium browser is found."""
    browser = find_app_browser()
    if browser:
        subprocess.Popen([
            browser, f"--app={url}",
            f"--user-data-dir={APP_PROFILE_DIR}",
            # Keep this a clean, isolated app instance. Without these, Edge
            # signs the profile into the Windows account and syncs the user's
            # extensions (Grammarly, etc.), each of which pops its own welcome
            # tab -- unwanted noise every launch. Disable extensions + sync so
            # only the ToDoChat window opens.
            "--disable-extensions", "--disable-sync",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=480,880",
        ])
    else:
        webbrowser.open(url)


def main():
    url = f"http://{HOST}:{PORT}/"
    # Reject a duplicate launch loudly instead of silently co-binding the port
    # (Windows SO_REUSEADDR would otherwise let two servers share port 8765).
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"ポート {PORT} は使用中です。ToDoChat は既に起動している可能性があります。")
        print(f"ウィンドウが見当たらない場合は {url} を開いてください。")
        open_app_window(url)
        return
    print(f"ToDoChat running at {url}")
    print(f"  CLI     : {CLI}")
    print(f"  current : {CONFIG['current']}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.8, lambda: open_app_window(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
