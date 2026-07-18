#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ToDoChat — minimal local web prototype (Pattern A: wraps the headless claude CLI).

Python standard library only. Spawns the `claude` CLI in non-interactive (-p) mode
using the Pro subscription auth, so there is no per-token API billing.
"""
import json
import os
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- config -----------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8765
APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent            # D:\seisaku\ToDoChat  (claude runs here)
TASKS_FILE = PROJECT_DIR / "TASKS.md"

# The headless CLI is installed at ~/.local/bin and is NOT on PATH, so use the
# full path. Fall back to a bare "claude" if that ever changes.
_NATIVE = Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "claude.exe"
CLI = str(_NATIVE) if _NATIVE.exists() else "claude"

PERSONA = (
    "あなたは「ToDoChat」という個人開発タスクアシスタントです。"
    "ユーザーが隙間時間に即座に開発へ取り掛かれるよう支援します。"
    "常に日本語で、要点を先に、簡潔に答えます。"
    "先回りして次の具体的なアクションを1つ提案してください。"
)
# v0 is advisory / read-only. Editing (Edit/Write/Bash) is a later step.
ALLOWED_TOOLS = ["Read", "Glob", "Grep"]
TIMEOUT_SEC = 240

# --- claude CLI wrapper -----------------------------------------------------
def run_claude(prompt, session_id=None):
    cmd = [
        CLI, "-p", prompt,
        "--output-format", "json",
        "--append-system-prompt", PERSONA,
        "--allowedTools", *ALLOWED_TOOLS,
    ]
    if session_id:
        cmd += ["--resume", session_id]
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_DIR),
            capture_output=True, timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "応答がタイムアウトしました。もう一度お試しください。"}
    except FileNotFoundError:
        return {"ok": False, "error": f"claude CLI が見つかりません: {CLI}"}

    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        return {"ok": False, "error": f"CLIから応答がありませんでした。\n{err[:500]}"}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"応答の解析に失敗しました。\n{out[:500]}"}
    if data.get("is_error"):
        return {"ok": False, "error": data.get("result") or "エラーが発生しました。"}
    u = data.get("usage") or {}
    return {
        "ok": True,
        "reply": data.get("result", ""),
        "session_id": data.get("session_id"),
        "usage": {
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
            "cost_usd": data.get("total_cost_usd", 0) or 0,
        },
    }


def read_tasks():
    try:
        return TASKS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "(TASKS.md が見つかりません)"


def init_greeting():
    prompt = (
        "以下は現在のタスク状況（TASKS.md）です:\n\n"
        f"{read_tasks()}\n\n"
        "起動時の第一声として、次の3点を短くまとめてください:\n"
        "(1) 現状のタスク状況を1〜2文で要約\n"
        "(2) 次に取り掛かるべきタスクを1つ提案\n"
        "(3) 他に優先すべきことがないかユーザーへ一言問いかけ"
    )
    return run_claude(prompt)


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

    def do_GET(self):
        if self.path == "/api/meta":
            self._send_json({"project": PROJECT_DIR.name, "path": str(PROJECT_DIR)})
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
            self._send_json(init_greeting())
        elif self.path == "/api/chat":
            body = self._read_body()
            msg = (body.get("message") or "").strip()
            if not msg:
                self._send_json({"ok": False, "error": "メッセージが空です。"})
                return
            self._send_json(run_claude(msg, session_id=body.get("session_id")))
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass  # keep the console quiet


def main():
    url = f"http://{HOST}:{PORT}/"
    # Reject a duplicate launch loudly instead of silently co-binding the port
    # (Windows SO_REUSEADDR would otherwise let two servers share port 8765).
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"ポート {PORT} は使用中です。ToDoChat は既に起動している可能性があります。")
        print(f"ブラウザで {url} を開いてください。")
        webbrowser.open(url)
        return
    print(f"ToDoChat running at {url}")
    print(f"  CLI     : {CLI}")
    print(f"  project : {PROJECT_DIR}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
