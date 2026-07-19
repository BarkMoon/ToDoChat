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
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid
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
# Three modes, chosen per-message from the UI:
#   advisory - read-only exploration, no file/shell access.
#   edit     - file editing (Edit/Write) confined to the working folder; no shell.
#   confirm  - file editing confined to the folder PLUS shell/app execution (Bash),
#              but every Bash command is approved by the user one at a time (the
#              guard hook calls back to /api/hook/permission and blocks until the
#              browser answers). This is the UI default.
#
# IMPORTANT safety note: in headless (-p) mode there is NO built-in classifier or
# path confinement. A tool listed in --allowedTools runs unconditionally and can
# write ANYWHERE on disk (verified: an allow-listed Write reaches absolute paths
# outside the folder). --permission-mode auto does not gate here either -- it just
# denies non-allow-listed tools. So the real boundary is a PreToolUse hook
# (app/edit_guard.py). Its deny is authoritative and overrides --allowedTools
# (verified), so Bash can be allow-listed in confirm mode while the hook still
# blocks every command the user hasn't approved.
ADVISORY_TOOLS = ["Read", "Glob", "Grep"]
EDIT_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write"]            # no Bash; hook confines writes
CONFIRM_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]  # Bash gated per-command by the hook
TOOLS_BY_MODE = {"advisory": ADVISORY_TOOLS, "edit": EDIT_TOOLS, "confirm": CONFIRM_TOOLS}
HOOK_MODES = ("edit", "confirm")   # modes that register the guard hook
GUARD_HOOK = APP_DIR / "edit_guard.py"
STREAM_INACTIVITY_TIMEOUT = 300    # kill claude after this many seconds of no output (paused while awaiting approval)
PERM_WAIT_TIMEOUT = 300            # how long the server waits for the user's allow/deny before denying


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

# --- per-command permission plumbing (confirm mode) -------------------------
# RUNS: active streaming turns, keyed by a run_id passed to the guard hook via
# env. Each holds a queue the streaming generator drains -- the hook endpoint
# pushes permission_request items onto it so they interleave with claude output.
# PENDING_PERMS: in-flight approval requests, keyed by perm_id; the hook thread
# blocks on the Event until the browser answers via /api/permission-response.
RUNS = {}
RUNS_LOCK = threading.Lock()
PENDING_PERMS = {}
PENDING_LOCK = threading.Lock()


def request_permission(run_id, tool, tool_input):
    """Called on the hook's HTTP thread. Surfaces an approval request into the
    active run's stream and blocks until the user answers (or we time out ->
    deny). Returns "allow" or "deny"."""
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if not run:
        return "deny"   # no active stream to ask through
    perm_id = uuid.uuid4().hex
    ev = threading.Event()
    with PENDING_LOCK:
        PENDING_PERMS[perm_id] = {"event": ev, "decision": None}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    run["perm_pending"] = True   # pause the inactivity watchdog while the user thinks
    run["queue"].put(("perm_request", {
        "id": perm_id, "tool": tool, "command": command, "input": tool_input,
    }))
    got = ev.wait(PERM_WAIT_TIMEOUT)
    run["perm_pending"] = False
    run["last"] = time.time()
    with PENDING_LOCK:
        rec = PENDING_PERMS.pop(perm_id, None)
    decision = (rec or {}).get("decision") if got else None
    return decision if decision in ("allow", "deny") else "deny"


def resolve_permission(perm_id, decision):
    """Called on the browser's HTTP thread to answer a pending request."""
    with PENDING_LOCK:
        rec = PENDING_PERMS.get(perm_id)
    if not rec:
        return {"ok": False, "error": "対象の許可リクエストが見つかりません（期限切れの可能性）。"}
    rec["decision"] = "allow" if decision == "allow" else "deny"
    rec["event"].set()
    return {"ok": True}


# --- shutdown-on-window-close plumbing --------------------------------------
# The app window has no OS process tie to this server, so closing it would
# otherwise leave the Python server (and the CLI it spawns) running. The page
# heartbeats /api/alive while open and beacons /api/shutdown on unload; we then
# shut down after a short grace period -- but abort if a fresh heartbeat arrives
# in the meantime, so a reload (which also fires unload) does NOT kill us.
SHUTDOWN_GRACE = 8.0          # seconds to wait after an unload beacon before quitting
SHUTDOWN_LOCK = threading.Lock()
_shutdown_timer = None
_arm_time = 0.0
_last_alive = 0.0
_SERVER = None                # set in main()


def note_alive():
    """A page is open and beating. Records the time so an armed shutdown that
    was triggered by a reload's unload gets cancelled when the new page loads."""
    global _last_alive
    _last_alive = time.time()


def arm_shutdown():
    """An unload beacon arrived (window closed OR reloaded). Schedule a shutdown
    check after the grace period; it only fires if no page checked in since."""
    global _shutdown_timer, _arm_time
    with SHUTDOWN_LOCK:
        _arm_time = time.time()
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
        _shutdown_timer = threading.Timer(SHUTDOWN_GRACE, _shutdown_check)
        _shutdown_timer.daemon = True
        _shutdown_timer.start()


def _shutdown_check():
    # A heartbeat after we armed means a page is still alive (a reload) -> abort.
    if _last_alive > _arm_time:
        return
    stop_server()


def stop_server():
    """Kill any in-flight CLI processes and stop the HTTP server, ending the
    process. Safe to call from a timer/other thread (not serve_forever's)."""
    with RUNS_LOCK:
        procs = [r.get("proc") for r in RUNS.values()]
    for p in procs:
        try:
            if p and p.poll() is None:
                p.kill()
        except OSError:
            pass
    if _SERVER is not None:
        threading.Thread(target=_SERVER.shutdown, daemon=True).start()


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
    """Generator yielding, as the reply is produced:
        {"type":"delta","text":...}                       streamed reply text
        {"type":"permission_request","id",...}            confirm mode: awaiting user approval
        {"type":"final", ok:..., reply/error, usage}      exactly one, at the end
    In confirm mode the guard hook blocks each Bash call and calls back to
    /api/hook/permission; request_permission pushes the request onto this run's
    queue so it interleaves with claude's output stream."""
    proj = CONFIG["current"]
    if not os.path.isdir(proj):
        yield {"type": "final", "ok": False, "error": f"作業フォルダが存在しません: {proj}"}
        return

    mode = mode if mode in TOOLS_BY_MODE else "advisory"
    model_alias = MODELS.get(model, MODELS[DEFAULT_MODEL])
    hook_mode = mode in HOOK_MODES
    run_id = uuid.uuid4().hex
    cmd = [
        CLI, "-p", prompt,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--append-system-prompt", PERSONA,
        "--allowedTools", *TOOLS_BY_MODE[mode],
        "--model", model_alias,
    ]
    env = None
    if hook_mode:
        # The guard hook is the enforcement boundary. The env vars tell it which
        # folder writes must stay inside, which mode it's in (edit denies Bash;
        # confirm asks), and how to reach us to request per-command approval.
        cmd += ["--settings", guard_settings_arg()]
        env = dict(
            os.environ,
            TODOCHAT_PROJECT_ROOT=proj,
            TODOCHAT_MODE=mode,
            TODOCHAT_RUN_ID=run_id,
            TODOCHAT_PERM_URL=f"http://{HOST}:{PORT}/api/hook/permission",
        )
    sid = SESSIONS.get(norm(proj)) if resume else None
    if sid:
        cmd += ["--resume", sid]

    try:
        # stderr is merged into stdout: reading only one pipe avoids the classic
        # subprocess deadlock. Stray non-JSON lines are skipped below.
        proc = subprocess.Popen(
            cmd, cwd=proj, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        yield {"type": "final", "ok": False, "error": f"claude CLI が見つかりません: {CLI}"}
        return

    q = queue.Queue()
    run = {"queue": q, "last": time.time(), "perm_pending": False, "timed_out": False, "proc": proc}
    with RUNS_LOCK:
        RUNS[run_id] = run

    def reader():
        try:
            for line in proc.stdout:
                run["last"] = time.time()
                line = line.strip()
                if line:
                    q.put(("line", line))
        finally:
            q.put(("done", None))

    def watchdog():
        while proc.poll() is None:
            time.sleep(5)
            if run["perm_pending"]:
                continue   # user is deciding -- don't count this as a hang
            if time.time() - run["last"] > STREAM_INACTIVITY_TIMEOUT:
                run["timed_out"] = True
                try:
                    proc.kill()
                except OSError:
                    pass
                return

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    result_data = None
    try:
        while True:
            kind, payload = q.get()
            if kind == "done":
                break
            if kind == "perm_request":
                yield {"type": "permission_request", **payload}
                continue
            try:
                d = json.loads(payload)
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
        with RUNS_LOCK:
            RUNS.pop(run_id, None)
        if proc.poll() is None:   # client disconnected mid-stream, etc.
            try:
                proc.kill()
            except OSError:
                pass

    if run["timed_out"]:
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
        "起動時の第一声として、次のフォーマットに厳密に従って出力してください。\n"
        "見出しタグは省略・変更せず、各見出しの下に内容を書いてください。\n\n"
        "【現在のタスク状況】\n"
        "(1〜2文で要約)\n\n"
        "【次のタスク】\n"
        "(次に取り掛かるべきタスクを1つ提案)\n\n"
        "【確認】\n"
        "(他に優先すべきことがないかユーザーへ一言問いかけ)\n\n"
        "TASKS.md が無い場合は、フォルダ内のコードやREADMEなどから状況を推測して要約してください。"
    )
    yield from run_claude_stream(prompt, resume=False, model=model, mode=mode)


# --- HTTP handler -----------------------------------------------------------
ERROR_LOG = APP_HOME / "server_error.log"


def log_error(msg):
    """Record an error to both the console and server_error.log. The launcher
    window can close before the user reads it, so the file copy guarantees the
    traceback survives and can be inspected afterwards."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (stamp, msg)
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def json_bytes(obj):
    """Serialize a response object to UTF-8 bytes, never raising on content.

    errors="replace" is essential: the claude CLI can emit lone surrogates or
    otherwise un-encodable characters on Windows (e.g. a multibyte Japanese git
    commit message that got mangled into a heredoc). A plain .encode("utf-8")
    then raises UnicodeEncodeError mid-stream, which killed the whole response
    before the final event was sent -- the browser only ever saw "不明なエラー".
    Replacing the bad char keeps the JSON valid and the stream alive."""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json_bytes(obj)
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
        flushing after each one so the browser sees deltas as they're produced.

        Any unexpected error is turned into a real 'final' error event (with the
        traceback) and also printed to the server console -- never a silent
        truncation, which the browser could only report as an opaque error."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def write(ev):
            self.wfile.write(json_bytes(ev) + b"\n")
            self.wfile.flush()

        try:
            for ev in events:
                write(ev)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # client navigated away / closed the tab mid-stream
        except Exception:
            tb = traceback.format_exc()
            log_error("stream error:\n" + tb)
            try:   # best effort: tell the browser what actually went wrong
                write({"type": "final", "ok": False,
                       "error": "サーバー内部エラーが発生しました:\n" + tb})
            except Exception:
                pass  # connection is probably gone too; log still has the trace

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
        elif self.path == "/api/hook/permission":
            # Called by the guard hook subprocess (confirm mode). Blocks until
            # the user answers, then returns the decision to the hook.
            body = self._read_body()
            decision = request_permission(body.get("run_id"), body.get("tool"), body.get("tool_input") or {})
            self._send_json({"decision": decision})
        elif self.path == "/api/permission-response":
            # Called by the browser when the user clicks 許可 / 拒否.
            body = self._read_body()
            self._send_json(resolve_permission(body.get("id"), body.get("decision")))
        elif self.path == "/api/projects/add":
            self._send_json(add_project(self._read_body().get("path")))
        elif self.path == "/api/projects/switch":
            self._send_json(switch_project(self._read_body().get("path")))
        elif self.path == "/api/projects/remove":
            self._send_json(remove_project(self._read_body().get("path")))
        elif self.path == "/api/projects/browse":
            self._send_json(browse_folder())
        elif self.path == "/api/alive":
            # Heartbeat from an open page; also cancels a reload-armed shutdown.
            note_alive()
            self._send_json({"ok": True})
        elif self.path == "/api/shutdown":
            # sendBeacon on window unload -> arm a graceful shutdown (a reload's
            # fresh /api/alive cancels it; a real close lets it proceed).
            arm_shutdown()
            self._send_json({"ok": True})
        elif self.path == "/api/quit":
            # The 終了 button: stop immediately.
            self._send_json({"ok": True})
            stop_server()
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
    global _SERVER
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
    _SERVER = server
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
