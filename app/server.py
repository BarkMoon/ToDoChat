#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ToDoChat — local web prototype (Pattern A: wraps the headless claude CLI).

Python standard library only. Spawns the `claude` CLI in non-interactive (-p) mode
using the Pro subscription auth, so there is no per-token API billing.

The "working folder" (the app being developed) is switchable at runtime; the list
of folders is persisted to projects.json next to this app.
"""
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from safe_shell import is_safe_command   # read-only-command allowlist (auto-approve)

# --- version ----------------------------------------------------------------
# SemVer 0.x while pre-1.0 (still in active development). Bump MINOR for new
# features / notable changes, PATCH for fixes; reserve 1.0.0 for "done enough".
APP_VERSION = "0.7.7"

# --- paths / config ---------------------------------------------------------
# HOST is the local-facing address used for the in-app window and for the hook
# callback URL (edit_guard / permission hooks connect from a local subprocess),
# so it must stay a loopback address. BIND_HOST is what the HTTP server actually
# listens on. The choice is driven by the in-app "LANモード" setting (persisted in
# projects.json as "lan_mode", default ON = 0.0.0.0 so a phone on the same Wi-Fi
# can connect). Exposing it on the LAN means anyone on the network can drive the
# CLI, so switch to ローカル限定 on untrusted networks (and add token auth before
# routine remote use). Env TODOCHAT_HOST, if set, overrides the in-app setting.
# BIND_HOST is resolved after CONFIG is loaded (see resolve_bind_host below).
HOST = "127.0.0.1"
_HOST_ENV = os.environ.get("TODOCHAT_HOST", "").strip()
PORT = int(os.environ.get("TODOCHAT_PORT", "8765"))
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
    "\n\n【コマンドの説明】シェルやアプリを実行する際、Bashツールの description "
    "フィールドは必ず日本語で記述してください。これは実行確認カードに『意図・処理内容』"
    "として表示され、ユーザーが許可/拒否を判断する材料になります。そのコマンドが"
    "何をするのか（目的と処理内容）を簡潔な日本語で書いてください（英語で書かないこと）。"
    "\n\n【記憶ログ】アプリ再起動後も引き継ぐべき『進行中タスクの状態・決定事項・"
    "次の一手・重要なパスや事実』がある場合に限り、返信の一番最後に次の形式の"
    "記憶ブロックを1つだけ付けてください（引き継ぐことが無ければ付けない）:\n"
    "[[TODOCHAT_MEMORY]]\n"
    "(数行の簡潔な要約のみ。会話の全文ログは書かない。トークン節約が目的)\n"
    "[[/TODOCHAT_MEMORY]]\n"
    "このブロックはユーザーには表示されず、次回起動時の引き継ぎメモとして保存されます。"
    "毎回付ける必要はなく、状況に変化があった時だけ最新の内容で上書きしてください。"
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
# Note: there is deliberately NO timeout on waiting for the user's allow/deny in
# confirm mode -- request_permission waits until the user answers (or the run's
# process dies). The inactivity watchdog above is paused while a request is pending.


def guard_settings_arg():
    """A --settings JSON string registering the edit-mode PreToolUse guard hook.
    Passed inline (the CLI accepts a JSON string, not just a file path).

    A large explicit `timeout` (seconds) is essential: in confirm mode the hook
    blocks while it waits for the user's allow/deny, and the user may be away from
    the machine. Without this, the CLI's own default PreToolUse hook timeout kicks
    in and abandons our still-waiting hook, letting the turn finish before the user
    ever answers -- which defeats the server-side no-deadline wait. Match that
    server-side backstop here (24h == "wait until the user answers")."""
    py = sys.executable.replace("\\", "/")
    guard = str(GUARD_HOOK).replace("\\", "/")
    command = '"%s" "%s"' % (py, guard)
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": command, "timeout": 86400},
        ]},
    ]}})

# Models selectable per-message from the UI. Keys are what the client sends;
# values are the --model alias the CLI accepts ("opus"/"sonnet"/"haiku").
MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}
DEFAULT_MODEL = "sonnet"

# Human-readable version names for the "which model did this work" memo, so a
# commit message can credit the actual model (see the contributor-memo section).
# Keyed by the same alias the UI sends. Kept next to MODELS so they stay in sync.
MODEL_DISPLAY = {"opus": "Opus 4.8", "sonnet": "Sonnet 5", "haiku": "Haiku 4.5"}
# Tools whose use means the model actually edited files (as opposed to read-only
# advice/verification). Only these turns get credited in the contributor memo.
EDIT_TOOL_NAMES = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_GIT_COMMIT_RE = re.compile(r"\bgit\b[^\n&|;]*\bcommit\b")


def _is_git_commit(command):
    """True if a shell command line invokes `git commit` (used to clear the
    contributor memo once its changes are committed)."""
    return bool(_GIT_COMMIT_RE.search(command or ""))


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


def lan_mode_enabled():
    """Whether the server should listen on the LAN. Default ON (LANモード) so a
    phone on the same Wi-Fi can connect out of the box; the user can switch to
    ローカル限定 in the settings window. Stored in projects.json as "lan_mode"."""
    return CONFIG.get("lan_mode") is not False


def resolve_bind_host():
    """Address the HTTP server listens on. An explicit TODOCHAT_HOST env var wins
    (advanced override); otherwise the in-app LANモード setting decides."""
    if _HOST_ENV:
        return _HOST_ENV
    return "0.0.0.0" if lan_mode_enabled() else HOST


BIND_HOST = resolve_bind_host()

# --- session-id persistence (for the optional full-log restore mode) --------
# SESSIONS maps norm(project_path) -> the CLI session_id of that project's live
# conversation. We ALWAYS mirror it to disk (sessions.json), independently of the
# full-log toggle, so that turning the toggle on and restarting can resume the
# real conversation. The toggle only controls STARTUP behaviour (resume the saved
# session vs. start fresh from the memory note) -- see init_greeting_stream.
# The CLI keeps its own full transcript on disk keyed by session_id, so a saved
# id stays resumable across server restarts; we only persist the pointer to it.
SESSIONS_FILE = APP_HOME / ".todochat" / "sessions.json"


def load_sessions():
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if v}
    except (FileNotFoundError, OSError, ValueError):
        pass
    return {}


def save_sessions():
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSIONS_FILE.write_text(
            json.dumps(SESSIONS, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def set_session(proj, sid):
    """Record a project's live session_id in memory and mirror it to disk."""
    if not sid:
        return
    SESSIONS[norm(proj)] = sid
    save_sessions()


def drop_session(proj):
    """Forget a project's session both in memory and on disk (used by /clear and
    project removal). No-op if there was nothing saved."""
    key = norm(proj)
    existed = SESSIONS.pop(key, None) is not None
    if existed:
        save_sessions()


SESSIONS = load_sessions()   # norm(project_path) -> claude session_id (mirrored to sessions.json)

# --- memory log (compact cross-restart hand-off note) -----------------------
# We deliberately do NOT persist/replay the full CLI transcript across restarts
# (that re-sends the whole history every turn -> heavy token use). Instead, the
# AI appends a small [[TODOCHAT_MEMORY]]...[[/TODOCHAT_MEMORY]] block to a reply
# only when there is in-progress work worth remembering; the server extracts it
# and stores just that summary, per project. On the next launch the greeting is
# seeded with this note (a few lines) instead of the whole conversation.
MEMORY_DIR = APP_HOME / ".todochat" / "memory"   # active notes (server-managed, gitignored)
TRASH_DIR = APP_HOME / ".todochat" / "trash"     # backups of deleted/overwritten notes
MEMORY_RE = re.compile(r"\[\[TODOCHAT_MEMORY\]\](.*?)\[\[/TODOCHAT_MEMORY\]\]", re.DOTALL)

# --- contributor memo (which models did the uncommitted work) ---------------
# A tiny per-project list of the model display-names that DID implementation work
# (design or an actual file edit) since the last commit. It is auto-appended when
# a turn issues an Edit/Write, injected into later turns so the AI can credit the
# right models in a commit message, and wiped when those changes are committed.
# Read-only advice/verification turns are never credited (they issue no edits).
# Like the memory notes it lives under ToDoChat's own .todochat/ (not the target
# project), so it never leaks into the project being developed.
MODELS_DIR = APP_HOME / ".todochat" / "models"   # per-project contributor lists (JSON)

# --- cross-device transcript (for the 🔄 sync button) -----------------------
# A per-project rolling log of the VISIBLE conversation (user + AI text only),
# so a chat started on one device (e.g. phone) can be pulled up and continued on
# another (e.g. PC) that hits the same server. The AI's context continuity is
# already shared server-side (session_id + memory note); this only fills the gap
# of the visible bubbles, which otherwise live solely in each browser's DOM.
# Capped to the most recent turns so the file can't grow without bound. Lives
# under ToDoChat's own .todochat/ (gitignored), never the target project.
TRANSCRIPTS_DIR = APP_HOME / ".todochat" / "transcripts"
MAX_TRANSCRIPT_TURNS = 200


def memory_path(proj):
    """Per-project memory file. Named from the folder's basename plus a short
    hash of its normalized path (readable, collision-free across folders)."""
    n = norm(proj)
    h = hashlib.sha1(n.encode("utf-8")).hexdigest()[:8]
    base = os.path.basename(os.path.normpath(str(proj))) or "root"
    safe = re.sub(r"[^\w.-]", "_", base)[:40]
    return MEMORY_DIR / f"{safe}-{h}.md"


def read_memory(proj):
    try:
        return memory_path(proj).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""


def backup_memory(proj):
    """Copy the current note into TRASH before it is overwritten or deleted.
    TRASH is NEVER read back by the server, so these copies never re-enter a
    prompt (zero token cost) -- they are just retained copies of deleted files,
    kept in case the user wants to recover something."""
    p = memory_path(proj)
    if not p.exists():
        return
    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        # uuid suffix so two backups in the same second don't overwrite.
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        shutil.copy2(p, TRASH_DIR / f"{p.stem}-{stamp}{p.suffix}")
    except OSError:
        pass


def apply_memory_block(proj, reply_text):
    """If a reply carries a memory block, persist its contents (backing up the
    prior note first) and return (stripped_reply, had_memory). had_memory is True
    whenever a non-empty block was present -- even if identical to the stored note
    (so /remember can confirm "captured" without needing a disk change). Only
    writes when the note actually changed, so identical re-emissions don't spam
    TRASH."""
    m = MEMORY_RE.search(reply_text or "")
    if not m:
        return reply_text, False
    new = m.group(1).strip()
    if new and new != read_memory(proj):
        backup_memory(proj)
        try:
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            memory_path(proj).write_text(new, encoding="utf-8")
        except OSError:
            pass
    return MEMORY_RE.sub("", reply_text).strip(), bool(new)


def clear_history(proj):
    """/clear: forget the live session pointer and delete this project's memory
    note (backing it up to TRASH first). The CLI's own transcript files are left
    on disk but are no longer referenced, so the next greeting starts fresh."""
    drop_session(proj)   # also removes the saved session_id (no stale full-log resume)
    clear_transcript(proj)   # the cross-device chat mirror goes too
    p = memory_path(proj)
    had = p.exists()
    backup_memory(proj)
    try:
        if had:
            p.unlink()
    except OSError:
        pass
    return {"ok": True, "cleared_memory": had}


def clear_log(proj):
    """/save & /refresh: drop the live session pointer and the cross-device
    transcript (the "full log"), but KEEP the memory note. Used right after a
    forced snapshot so the heavy conversation log is shed while the compact
    hand-off summary survives -- the next start then resumes from that note
    instead of replaying everything."""
    drop_session(proj)       # no stale session_id -> next start is fresh
    clear_transcript(proj)   # the cross-device chat mirror goes too
    return {"ok": True}


def get_memory(proj):
    """Return the current project's hand-off note for the viewer/editor UI."""
    return {"ok": True, "memory": read_memory(proj), "exists": memory_path(proj).exists()}


def write_memory(proj, text):
    """Manual edit save from the editor UI. Backs the old note up to TRASH first
    (same as every other overwrite/delete path), then writes the new text -- or
    deletes the note entirely when the text is blank, so clearing the box in the
    editor is the natural way to remove the note."""
    text = (text or "").strip()
    backup_memory(proj)
    p = memory_path(proj)
    try:
        if text:
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        elif p.exists():
            p.unlink()
    except OSError as e:
        return {"ok": False, "error": f"保存に失敗しました: {e}"}
    return {"ok": True, "memory": read_memory(proj), "exists": bool(text)}


def contributors_path(proj):
    """Per-project contributor-memo file (same naming scheme as memory_path)."""
    n = norm(proj)
    h = hashlib.sha1(n.encode("utf-8")).hexdigest()[:8]
    base = os.path.basename(os.path.normpath(str(proj))) or "root"
    safe = re.sub(r"[^\w.-]", "_", base)[:40]
    return MODELS_DIR / f"{safe}-{h}.json"


def read_contributors(proj):
    try:
        data = json.loads(contributors_path(proj).read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()]
    except (FileNotFoundError, OSError, ValueError):
        pass
    return []


def save_contributors(proj, models):
    """Write the list (deduped, order preserved). An empty list deletes the file."""
    seen, out = set(), []
    for m in models:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    p = contributors_path(proj)
    try:
        if out:
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        elif p.exists():
            p.unlink()
    except OSError:
        pass
    return out


def add_contributor(proj, model_key):
    """Credit a model (by UI alias) for editing files this turn. No-op for an
    unknown alias or one already recorded."""
    name = MODEL_DISPLAY.get(model_key)
    if not name:
        return
    cur = read_contributors(proj)
    if name not in cur:
        save_contributors(proj, cur + [name])


def clear_contributors(proj):
    p = contributors_path(proj)
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass


def get_contributors(proj):
    """Return the current project's contributor list for the viewer/editor UI."""
    return {"ok": True, "models": read_contributors(proj)}


def write_contributors_text(proj, text):
    """Manual edit save from the editor UI: split on newlines/commas into a list
    (blank clears the memo)."""
    items = re.split(r"[\n,、]+", text or "")
    return {"ok": True, "models": save_contributors(proj, items)}


def contributor_commit_note(proj):
    """A compact one-line reminder injected into a turn's prompt so the AI credits
    the right models when it writes a commit message. Empty when nothing is
    pending (so it costs no tokens outside active work)."""
    models = read_contributors(proj)
    if not models:
        return ""
    return (
        "【使用モデル記録】現在の未コミットの変更に貢献したモデル: "
        + "、".join(models) + "。"
        "git commit を行う際は、CLAUDE.md のスタイル規約に従い、コミットメッセージ末尾の "
        "(モデル名) にこれらを記載してください（複数なら『+』で連結）。この行はユーザーへ表示不要です。\n\n"
    )


def transcript_path(proj):
    """Per-project transcript file (same naming scheme as memory_path)."""
    n = norm(proj)
    h = hashlib.sha1(n.encode("utf-8")).hexdigest()[:8]
    base = os.path.basename(os.path.normpath(str(proj))) or "root"
    safe = re.sub(r"[^\w.-]", "_", base)[:40]
    return TRANSCRIPTS_DIR / f"{safe}-{h}.json"


def read_transcript(proj):
    """Return this project's stored conversation as a list of
    {"role": "user"|"ai", "text": str, "ts": float}."""
    try:
        data = json.loads(transcript_path(proj).read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and d.get("text")]
    except (FileNotFoundError, OSError, ValueError):
        pass
    return []


def append_transcript(proj, role, text):
    """Append one turn to the project's transcript, trimming to the most recent
    MAX_TRANSCRIPT_TURNS. Best-effort: never raises into the streaming path."""
    text = (text or "").strip()
    if not text:
        return
    items = read_transcript(proj)
    items.append({"role": role, "text": text, "ts": time.time()})
    if len(items) > MAX_TRANSCRIPT_TURNS:
        items = items[-MAX_TRANSCRIPT_TURNS:]
    try:
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        transcript_path(proj).write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def clear_transcript(proj):
    """Delete a project's transcript (used by /clear). No backup: it's a
    convenience mirror of chat bubbles, not authored content."""
    try:
        transcript_path(proj).unlink()
    except (FileNotFoundError, OSError):
        pass


def get_transcript(proj):
    """Return the stored transcript for the 🔄 sync button to replay."""
    return {"ok": True, "turns": read_transcript(proj)}


# /remember: force a manual snapshot of the current session into the memory note
# now, regardless of whether the AI judged the state worth saving on its own. We
# resume the live session so the summary reflects the actual conversation, ask
# for the memory block ONLY (no chit-chat), and let the normal apply_memory_block
# path persist it. Advisory mode = read-only, so no approval cards interrupt it.
REMEMBER_PROMPT = (
    "【手動スナップショット要求 /remember】\n"
    "ユーザーが現在の状態を記憶ログへ手動で保存するよう要求しました。\n"
    "これまでの会話と作業状況をふまえ、アプリ再起動後に引き継ぐべき"
    "『進行中タスクの状態・決定事項・次の一手・重要なパスや事実』を数行で簡潔に要約し、"
    "必ず次の形式の記憶ブロックだけを出力してください。"
    "前置き・後書き・その他の文章は一切書かないでください:\n"
    "[[TODOCHAT_MEMORY]]\n"
    "(数行の簡潔な要約)\n"
    "[[/TODOCHAT_MEMORY]]"
)


def remember_stream(model=None):
    """Drive a forced snapshot turn and report whether it was saved. Yields the
    same event stream as a normal turn (deltas are hidden client-side, since we
    asked for block-only output), but the final event carries `remembered` and
    the saved `memory` text so the UI can confirm and echo it."""
    proj = CONFIG["current"]
    if not os.path.isdir(proj):
        yield {"type": "final", "ok": False, "error": f"作業フォルダが存在しません: {proj}"}
        return
    for ev in run_claude_stream(REMEMBER_PROMPT, resume=True, model=model, mode="advisory"):
        if ev.get("type") == "final" and ev.get("ok"):
            saved = ev.get("memory_saved", False)
            reply = (ev.get("reply") or "").strip()
            # Fallback: the model answered but skipped the markers -> treat the
            # whole reply as the snapshot so /remember never silently no-ops.
            if not saved and reply:
                backup_memory(proj)
                try:
                    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
                    memory_path(proj).write_text(reply, encoding="utf-8")
                    saved = True
                except OSError:
                    pass
            ev = dict(ev, remembered=saved, memory=read_memory(proj))
        yield ev


def snapshot_memory_blocking(proj, timeout=120):
    """Synchronous (non-streaming) forced snapshot, used by the auto-remember-on-
    close feature. There is no browser to stream to at shutdown, so we spawn the
    CLI, wait for the single JSON result, and persist it via apply_memory_block.

    Uses haiku to keep shutdown quick (the note is a short summary), resumes the
    live session so it reflects the conversation, and is skipped entirely when no
    session exists yet (nothing meaningful to remember). Best-effort: never
    raises; returns True if a note was saved."""
    if not os.path.isdir(proj):
        return False
    sid = SESSIONS.get(norm(proj))
    if not sid:
        return False   # no conversation happened -> nothing worth snapshotting
    cmd = [
        CLI, "-p", REMEMBER_PROMPT,
        "--output-format", "json",
        "--append-system-prompt", PERSONA,
        "--allowedTools", *ADVISORY_TOOLS,
        "--model", MODELS.get("haiku", MODELS[DEFAULT_MODEL]),
        "--resume", sid,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=proj, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        data = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    if not isinstance(data, dict) or data.get("is_error"):
        return False
    reply = data.get("result", "") or ""
    _, had = apply_memory_block(proj, reply)
    if not had and reply.strip():
        # No markers -> treat the whole reply as the snapshot (same fallback as
        # remember_stream) so an enabled toggle never silently saves nothing.
        try:
            backup_memory(proj)
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            memory_path(proj).write_text(reply.strip(), encoding="utf-8")
            had = True
        except OSError:
            pass
    return had


def finalize_memory_if_enabled():
    """Run the auto-close snapshot iff the toggle is on. Called on every real
    shutdown path (the 終了 button and a window close), never on a reload."""
    if not CONFIG.get("auto_remember"):
        return False
    try:
        return snapshot_memory_blocking(CONFIG["current"])
    except Exception:
        return False

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
    deny). Returns "allow" or "deny".

    Read-only commands on the safe_shell allowlist (git log/status, head, echo,
    ...) are AUTO-approved: the card is still pushed to the stream so the user
    sees what ran, but we return "allow" immediately instead of blocking for a
    click."""
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if not run:
        return "deny"   # no active stream to ask through
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if tool == "Bash" and is_safe_command(command):
        run["queue"].put(("perm_request", {
            "id": uuid.uuid4().hex, "tool": tool, "command": command,
            "input": tool_input, "auto": True,
        }))
        return "allow"
    perm_id = uuid.uuid4().hex
    ev = threading.Event()
    with PENDING_LOCK:
        PENDING_PERMS[perm_id] = {"event": ev, "decision": None}
    run["perm_pending"] = True   # pause the inactivity watchdog while the user thinks
    # `description` is the CLI's own plain-language summary of the command's intent
    # (Claude fills it into every Bash tool call); we surface it on the card so the
    # user sees WHAT the command does and WHY, not just the raw shell line. Only
    # needed here -- auto-approved safe commands don't get an explanation.
    desc = tool_input.get("description", "") if isinstance(tool_input, dict) else ""
    run["queue"].put(("perm_request", {
        "id": perm_id, "tool": tool, "command": command, "input": tool_input,
        "desc": desc,
    }))
    # Wait for the user's decision with NO hard deadline. Confirm mode exists so a
    # human can take their time (they may be away from the machine), so we never
    # auto-deny on a timer. We only stop waiting if the run is torn down or its CLI
    # process has exited -- e.g. the tab was closed mid-stream, which kills the
    # process (and this hook's child process with it) -- so this thread can't leak.
    proc = run.get("proc")
    answered = False
    while True:
        if ev.wait(1.0):
            answered = True
            break
        if proc is not None and proc.poll() is not None:
            break            # CLI process is gone; nobody can answer anymore
        with RUNS_LOCK:
            if RUNS.get(run_id) is not run:
                break        # this run was cleaned up
    run["perm_pending"] = False
    run["last"] = time.time()
    with PENDING_LOCK:
        rec = PENDING_PERMS.pop(perm_id, None)
    decision = (rec or {}).get("decision") if answered else None
    decision = decision if decision in ("allow", "deny") else "deny"
    # A committed change is now recorded; wipe the per-project "which models did
    # the work" memo so the next batch of changes starts a fresh contributor list
    # (the commit message the user just approved already carries the old names).
    if decision == "allow" and tool == "Bash" and _is_git_commit(command):
        clear_contributors(CONFIG["current"])
    return decision


def record_denial(run_id, tool, tool_input, reason):
    """Called by the guard hook (on its HTTP thread) whenever it denies a tool
    call. Stores a detailed record on the active run so the turn's final event can
    tell the user exactly WHAT was blocked (tool + command/path) and WHY, and logs
    it durably to the console / server_error.log for later inspection."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    target = (ti.get("command") or ti.get("file_path") or ti.get("notebook_path")
              or ti.get("path") or ti.get("url") or "")
    rec = {"tool": tool or "(不明)", "target": target,
           "reason": reason or "", "ts": time.time()}
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if run is not None:
        run["denials"].append(rec)
    where = f" [{target}]" if target else ""
    log_error(f"guard denied: {rec['tool']}{where} -> {rec['reason']}")
    return {"ok": True}


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
    # Real window close: optionally snapshot memory before the process exits.
    # (The page is already gone, so this MUST happen server-side.)
    finalize_memory_if_enabled()
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


# --- version ----------------------------------------------------------------
def version_string():
    """Display version. In a git checkout, append a dev identifier: the short
    commit hash plus a '+' marker when there are uncommitted changes
    (e.g. '0.7.0 (a4305a8)' committed, '0.7.0 (a4305a8+)' with local edits).
    Outside git, returns the bare version."""
    try:
        head = subprocess.run(
            ["git", "-C", str(APP_HOME), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3)
        if head.returncode != 0:
            return APP_VERSION
        commit = head.stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(APP_HOME), "status", "--porcelain"],
            capture_output=True, text=True, timeout=3)
        mark = "+" if dirty.stdout.strip() else ""
        return f"{APP_VERSION} ({commit}{mark})"
    except (OSError, subprocess.TimeoutExpired):
        return APP_VERSION


# --- project management -----------------------------------------------------
def list_projects():
    return {"ok": True, "current": CONFIG["current"], "projects": CONFIG["projects"],
            "auto_remember": bool(CONFIG.get("auto_remember")),
            "full_log": bool(CONFIG.get("full_log")),
            "enter_to_send": bool(CONFIG.get("enter_to_send")),
            "lan_mode": lan_mode_enabled(),
            # True when TODOCHAT_HOST env pins the address -> in-app toggle is moot
            # this run; UI disables the control and explains why.
            "lan_env_override": bool(_HOST_ENV),
            # What the server is ACTUALLY listening on right now, so the UI can tell
            # the user whether a changed setting still needs a restart to take hold.
            "lan_active": BIND_HOST not in ("127.0.0.1", "localhost"),
            # Firewall inbound-rule presence (True/False/None) for the status line.
            "firewall_rule": _firewall_rule_exists(),
            "version": version_string()}


def set_auto_remember(enabled):
    """Persist the 'snapshot memory on window close' toggle into projects.json so
    the button restores its last state on the next launch."""
    CONFIG["auto_remember"] = bool(enabled)
    save_config(CONFIG)
    return {"ok": True, "enabled": CONFIG["auto_remember"]}


def set_full_log(enabled):
    """Persist the 'full-log restore on startup' toggle into projects.json. Global
    (one setting for all projects) and default OFF, so the last choice is restored
    on the next launch. When ON, the opening greeting resumes the saved session
    (full conversation in context) instead of the compact memory note -- heavier on
    tokens, so it stays opt-in."""
    CONFIG["full_log"] = bool(enabled)
    save_config(CONFIG)
    return {"ok": True, "enabled": CONFIG["full_log"]}


def set_lan_mode(enabled):
    """Persist the LANモード toggle into projects.json. Global and default ON
    (0.0.0.0 = accept LAN clients). Takes effect on the NEXT launch because the
    listen address is bound once at startup, so the response reports whether a
    restart is still needed to match the new setting.

    Switching also syncs the Windows firewall inbound rule: LANモード adds the
    port allow-rule (initial setup), ローカル限定 removes it (cleanup). The config
    flag is saved first and unconditionally, so a cancelled/failed UAC prompt
    still records the user's choice -- the firewall result is reported separately
    for the UI to surface."""
    enabled = bool(enabled)
    CONFIG["lan_mode"] = enabled
    save_config(CONFIG)
    firewall = _sync_firewall_for_mode(enabled)
    return {"ok": True, "enabled": enabled,
            "active": BIND_HOST not in ("127.0.0.1", "localhost"),
            "env_override": bool(_HOST_ENV),
            "firewall": firewall}


def set_enter_to_send(enabled):
    """Persist the Enter-key behaviour into projects.json. Default (False) means
    Enter inserts a newline and Ctrl+Enter sends -- chosen to avoid accidental
    sends (especially mid-IME-composition and on phones). True restores the
    classic Enter-sends / Shift+Enter-newline behaviour."""
    CONFIG["enter_to_send"] = bool(enabled)
    save_config(CONFIG)
    return {"ok": True, "enabled": CONFIG["enter_to_send"]}


STARTUP_TASK_NAME = "ToDoChat"


# Windows error code returned when the user dismisses the UAC prompt.
_UAC_CANCELLED = 1223


def _schtasks(args):
    """Run schtasks.exe unelevated and return (returncode, stdout, stderr).
    Used for the read-only /query (which standard users are allowed to do)."""
    try:
        r = subprocess.run(["schtasks.exe", *args], capture_output=True,
                            text=True, timeout=10)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 1, "", "schtasks.exe が見つかりません（Windows専用機能です）。"
    except subprocess.TimeoutExpired:
        return 1, "", "schtasksの実行がタイムアウトしました。"


def _schtasks_elevated(argstr):
    """Run `schtasks <argstr>` elevated via a one-shot UAC prompt and return its
    exit code. Creating/deleting a Task Scheduler task needs admin rights even
    with /rl limited, so we self-elevate only for this click (PowerShell
    Start-Process -Verb RunAs) rather than running the whole app as admin. The
    argstr is passed as a single verbatim argument line so schtasks' own quoting
    (e.g. the quoted /tr path) survives Start-Process untouched.

    Returns _UAC_CANCELLED if the user dismisses the UAC dialog. Only the exit
    code is available (the elevated process is a separate hidden window), so
    callers map non-zero to a generic failure message."""
    ps = (
        "$ErrorActionPreference='Stop';"
        "try {"
        f"  $p = Start-Process -FilePath schtasks.exe -ArgumentList '{argstr}'"
        "   -Verb RunAs -Wait -PassThru -WindowStyle Hidden;"
        "  exit $p.ExitCode"
        "} catch {"
        f"  exit {_UAC_CANCELLED}"
        "}"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=120)
        return r.returncode
    except FileNotFoundError:
        return -1
    except subprocess.TimeoutExpired:
        return -2


# --- Windows firewall rule for LAN mode -------------------------------------
# LANモード (0.0.0.0) is useless if Windows Firewall drops inbound connections,
# so switching modes also sets up / tears down a single inbound-allow rule for
# our port. The rule is scoped to profile=private (home Wi-Fi etc.) as the safe
# default. Creating/deleting a firewall rule needs admin, so we self-elevate for
# that one click via the same one-shot UAC path used for startup registration.
FIREWALL_RULE_NAME = "ToDoChat"


def _firewall_rule_exists():
    """True/False if the ToDoChat inbound rule is present, or None if netsh is
    unavailable / the check failed (non-Windows, etc.). Read-only, so it runs
    unelevated -- standard users may query firewall rules."""
    try:
        # Discard output (don't decode it): netsh prints localized text (cp932 on
        # Japanese Windows) that can crash text-mode capture, and we only need the
        # exit code -- 0 when a matching rule exists, 1 ("No rules match...") when
        # none do, reliable regardless of locale.
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={FIREWALL_RULE_NAME}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    return r.returncode == 0


def _netsh_elevated(argstr):
    """Run `netsh <argstr>` elevated via a one-shot UAC prompt; return its exit
    code. Mirrors _schtasks_elevated: the argstr is one verbatim argument line so
    netsh's own tokens survive Start-Process untouched. Returns _UAC_CANCELLED if
    the user dismisses the UAC dialog."""
    ps = (
        "$ErrorActionPreference='Stop';"
        "try {"
        f"  $p = Start-Process -FilePath netsh.exe -ArgumentList '{argstr}'"
        "   -Verb RunAs -Wait -PassThru -WindowStyle Hidden;"
        "  exit $p.ExitCode"
        "} catch {"
        f"  exit {_UAC_CANCELLED}"
        "}"
    )
    try:
        # We only care about the exit code; discard output to avoid decoding
        # localized text (see _firewall_rule_exists).
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return r.returncode
    except FileNotFoundError:
        return -1
    except subprocess.TimeoutExpired:
        return -2


def _firewall_result(code, ok_msg):
    """Map a _netsh_elevated exit code to a UI-friendly firewall result dict."""
    if code == 0:
        return {"attempted": True, "ok": True, "cancelled": False, "message": ok_msg}
    if code == _UAC_CANCELLED:
        return {"attempted": True, "ok": False, "cancelled": True,
                "message": "管理者確認がキャンセルされたため、ファイアウォール設定は変更していません。"}
    return {"attempted": True, "ok": False, "cancelled": False,
            "message": "ファイアウォール設定の変更に失敗しました（管理者権限が必要です）。"}


def _sync_firewall_for_mode(lan):
    """Add (LANモード) or remove (ローカル限定) the inbound firewall rule to match
    the chosen mode. Only prompts for UAC when a change is actually needed -- if
    the rule is already in the desired state we do nothing (so re-selecting the
    same mode won't nag). Returns a result dict for the UI (or None when the
    firewall can't be managed on this machine)."""
    exists = _firewall_rule_exists()
    if exists is None:
        return {"attempted": False, "ok": False, "cancelled": False,
                "message": "ファイアウォールを操作できませんでした（netsh 不可）。手動設定が必要な場合があります。"}
    if lan:
        if exists:
            return {"attempted": False, "ok": True, "cancelled": False,
                    "message": "ファイアウォールの受信許可は設定済みです。"}
        argstr = (f"advfirewall firewall add rule name={FIREWALL_RULE_NAME} "
                  f"dir=in action=allow protocol=TCP localport={PORT} profile=private")
        return _firewall_result(
            _netsh_elevated(argstr),
            f"ファイアウォールにポート{PORT}の受信許可（プライベート）を追加しました。")
    else:
        if not exists:
            return {"attempted": False, "ok": True, "cancelled": False,
                    "message": "ファイアウォールの受信許可ルールはありません（クリーンです）。"}
        argstr = f"advfirewall firewall delete rule name={FIREWALL_RULE_NAME}"
        return _firewall_result(
            _netsh_elevated(argstr),
            "ファイアウォールの受信許可ルールを削除しました。")


def get_startup_status():
    code, _, _ = _schtasks(["/query", "/tn", STARTUP_TASK_NAME])
    return {"ok": True, "registered": code == 0}


def register_startup():
    """Register a log-on-trigger Task Scheduler task that runs start.bat, so
    ToDoChat launches automatically at Windows sign-in. The task itself runs
    with /rl limited (standard-user privileges) — only creating it needs the
    one-time UAC elevation below."""
    bat = APP_HOME / "start.bat"
    if not bat.is_file():
        return {"ok": False, "error": f"start.bat が見つかりません: {bat}"}
    # schtasks /tr wants the program path double-quoted (spaces in the install
    # path); the \" escapes keep those quotes literal inside PowerShell's
    # single-quoted ArgumentList string.
    argstr = (f'/create /tn {STARTUP_TASK_NAME} /tr "\\"{bat}\\"" '
              f'/sc onlogon /rl limited /f')
    code = _schtasks_elevated(argstr)
    if code == _UAC_CANCELLED:
        return {"ok": False, "cancelled": True,
                "error": "登録がキャンセルされました（管理者の許可が必要です）。"}
    if code != 0:
        return {"ok": False, "error": f"登録に失敗しました（コード {code}）。"}
    return {"ok": True, "registered": True}


def unregister_startup():
    argstr = f"/delete /tn {STARTUP_TASK_NAME} /f"
    code = _schtasks_elevated(argstr)
    if code == _UAC_CANCELLED:
        return {"ok": False, "cancelled": True,
                "error": "解除がキャンセルされました（管理者の許可が必要です）。"}
    if code != 0:
        return {"ok": False, "error": f"解除に失敗しました（コード {code}）。"}
    return {"ok": True, "registered": False}


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
    if SESSIONS.pop(key, None) is not None:
        save_sessions()
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


def run_claude_stream(prompt, resume=True, model=None, mode=None, inject_contrib=False):
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
    model_key = model if model in MODELS else DEFAULT_MODEL
    model_alias = MODELS[model_key]
    hook_mode = mode in HOOK_MODES
    # Prepend the contributor reminder (real chat turns only) so the AI can credit
    # the right models in any commit it writes. Empty unless work is pending.
    if inject_contrib:
        prompt = contributor_commit_note(proj) + prompt
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
            TODOCHAT_DENY_URL=f"http://{HOST}:{PORT}/api/hook/denial",
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
    run = {"queue": q, "last": time.time(), "perm_pending": False, "timed_out": False,
           "proc": proc, "denials": []}   # denials: detailed guard-hook block records
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
    did_edit = False   # did this turn actually issue a file-editing tool call?
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
            elif t == "assistant":
                # Full assistant message: note if it edited files, to credit the
                # model in the contributor memo (read-only turns issue no edits).
                for block in ((d.get("message") or {}).get("content") or []):
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") in EDIT_TOOL_NAMES):
                        did_edit = True
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
        set_session(proj, new_sid)   # mirror to sessions.json for full-log restore
    if did_edit:
        add_contributor(proj, model_key)   # credit this model for the file changes
    reply, had_memory = apply_memory_block(proj, result_data.get("result", ""))
    usage = extract_usage(result_data)
    # Prefer our own detailed denial records (tool + command/path + reason, captured
    # by the guard hook) over the CLI's bare permission_denials, so the UI can show
    # exactly what was blocked and why. Fall back to the CLI list if we captured none.
    if run["denials"]:
        usage["permission_denials"] = run["denials"]
    yield {"type": "final", "ok": True, "reply": reply,
           "usage": usage, "memory_saved": had_memory}


def read_tasks(d):
    try:
        return (Path(d) / "TASKS.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return "(このフォルダに TASKS.md はありません)"


# Light continuation prompt used only in full-log restore mode: the whole prior
# conversation is already back in context via --resume, so we don't re-inject
# TASKS.md or the memory note (that would double the tokens). We just ask for the
# same 3-section opening so the UI looks identical to a normal launch.
CONTINUE_PROMPT = (
    "アプリを再起動し、前回までの会話の続きから再開します。"
    "これまでの会話の文脈をふまえ、起動時の第一声として次のフォーマットに"
    "厳密に従って出力してください。見出しタグは省略・変更しないでください。\n\n"
    "【現在のタスク状況】\n"
    "(1〜2文で要約)\n\n"
    "【次のタスク】\n"
    "(次に取り掛かるべきタスクを1つ提案)\n\n"
    "【確認】\n"
    "(他に優先すべきことがないかユーザーへ一言問いかけ)"
)


def init_greeting_stream(model=None, mode=None):
    proj = CONFIG["current"]
    if not os.path.isdir(proj):
        yield {"type": "final", "ok": False, "error": f"作業フォルダが存在しません: {proj}"}
        return

    # Full-log restore (opt-in, default OFF): if the toggle is on AND we have a
    # saved session_id for this folder, resume the whole conversation instead of
    # starting fresh from the compact memory note. Heavier on tokens, so it's
    # opt-in. On any resume failure (stale/invalid id) fall back to a fresh
    # greeting so a broken pointer never blocks startup.
    if CONFIG.get("full_log") and SESSIONS.get(norm(proj)):
        saw_delta = False
        failed = False
        for ev in run_claude_stream(CONTINUE_PROMPT, resume=True, model=model, mode=mode):
            if ev.get("type") == "delta":
                saw_delta = True
                yield ev
            elif ev.get("type") == "final" and not ev.get("ok") and not saw_delta:
                failed = True   # resume errored before producing any text -> fall back
                break
            else:
                yield ev
        if not failed:
            return
        drop_session(proj)   # the saved id was unusable; don't retry it next time
        yield {"type": "notice",
               "text": "フルログを復元できなかったため、通常の起動に切り替えました。"}
        # fall through to the fresh greeting below

    SESSIONS.pop(norm(proj), None)   # start a fresh conversation for this folder
    mem = read_memory(proj)
    mem_section = (
        "以下は前回までの『引き継ぎメモ（記憶ログ）』です。"
        "これを最優先で踏まえて状況を把握してください:\n\n"
        f"{mem}\n\n"
    ) if mem else ""
    prompt = (
        f"作業対象フォルダ: {proj}\n\n"
        f"{mem_section}"
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


def recording_stream(proj, gen, user_text=None):
    """Wrap a chat/init event generator so the visible conversation is mirrored
    into the project's transcript (for the 🔄 cross-device sync). Records the
    user's message up front and the AI's memory-stripped reply when the turn
    finishes. Recording is best-effort and never interferes with the stream."""
    if user_text:
        append_transcript(proj, "user", user_text)
    for ev in gen:
        if ev.get("type") == "final" and ev.get("ok"):
            append_transcript(proj, "ai", ev.get("reply", ""))
        yield ev


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
        if self.path == "/api/startup/status":
            self._send_json(get_startup_status())
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
            proj = CONFIG["current"]
            self._stream_ndjson(recording_stream(
                proj, init_greeting_stream(model=body.get("model"), mode=body.get("mode"))))
        elif self.path == "/api/chat":
            body = self._read_body()
            msg = (body.get("message") or "").strip()
            if not msg:
                self._send_json({"ok": False, "error": "メッセージが空です。"})
                return
            proj = CONFIG["current"]
            self._stream_ndjson(recording_stream(
                proj,
                run_claude_stream(msg, resume=True, model=body.get("model"),
                                  mode=body.get("mode"), inject_contrib=True),
                user_text=msg))
        elif self.path == "/api/hook/permission":
            # Called by the guard hook subprocess (confirm mode). Blocks until
            # the user answers, then returns the decision to the hook.
            body = self._read_body()
            decision = request_permission(body.get("run_id"), body.get("tool"), body.get("tool_input") or {})
            self._send_json({"decision": decision})
        elif self.path == "/api/hook/denial":
            # Called by the guard hook when it denies a tool call, so the turn can
            # report exactly what was blocked and why (detailed block log).
            body = self._read_body()
            self._send_json(record_denial(body.get("run_id"), body.get("tool"),
                                          body.get("tool_input") or {}, body.get("reason")))
        elif self.path == "/api/permission-response":
            # Called by the browser when the user clicks 許可 / 拒否.
            body = self._read_body()
            self._send_json(resolve_permission(body.get("id"), body.get("decision")))
        elif self.path == "/api/remember":
            # /remember typed in chat: force a manual snapshot of the current
            # session into the memory note now (streamed like a normal turn).
            body = self._read_body()
            self._stream_ndjson(remember_stream(model=body.get("model")))
        elif self.path == "/api/memory/get":
            # Memory-editor modal opened: return the current project's note.
            self._send_json(get_memory(CONFIG["current"]))
        elif self.path == "/api/memory/save":
            # Memory-editor 保存/削除: overwrite (or clear) the note by hand.
            self._send_json(write_memory(CONFIG["current"], self._read_body().get("text")))
        elif self.path == "/api/models/get":
            # Contributor-memo modal opened: return the current project's model list.
            self._send_json(get_contributors(CONFIG["current"]))
        elif self.path == "/api/models/save":
            # Contributor-memo 保存/削除: overwrite (or clear) the model list by hand.
            self._send_json(write_contributors_text(CONFIG["current"], self._read_body().get("text")))
        elif self.path == "/api/clear":
            # /clear typed in chat: drop the live session + delete the memory
            # note for the current project (a backup is kept in TRASH first).
            self._send_json(clear_history(CONFIG["current"]))
        elif self.path == "/api/clear-log":
            # /save & /refresh: drop the full log (transcript + session) but keep
            # the memory note (which the caller has just refreshed via /remember).
            self._send_json(clear_log(CONFIG["current"]))
        elif self.path == "/api/projects/add":
            self._send_json(add_project(self._read_body().get("path")))
        elif self.path == "/api/projects/switch":
            self._send_json(switch_project(self._read_body().get("path")))
        elif self.path == "/api/projects/remove":
            self._send_json(remove_project(self._read_body().get("path")))
        elif self.path == "/api/projects/browse":
            self._send_json(browse_folder())
        elif self.path == "/api/auto-remember":
            # Toggle "snapshot memory on window close"; persisted to projects.json.
            self._send_json(set_auto_remember(self._read_body().get("enabled")))
        elif self.path == "/api/full-log":
            # Toggle "resume the full conversation log on startup"; persisted.
            self._send_json(set_full_log(self._read_body().get("enabled")))
        elif self.path == "/api/enter-mode":
            # Toggle Enter-key behaviour (Enter sends vs. Ctrl+Enter sends); persisted.
            self._send_json(set_enter_to_send(self._read_body().get("enabled")))
        elif self.path == "/api/lan-mode":
            # Toggle LANモード (listen on 0.0.0.0 vs loopback); persisted, next launch.
            self._send_json(set_lan_mode(self._read_body().get("enabled")))
        elif self.path == "/api/transcript/get":
            # 🔄 sync button: return the current project's cross-device transcript.
            self._send_json(get_transcript(CONFIG["current"]))
        elif self.path == "/api/startup/register":
            self._send_json(register_startup())
        elif self.path == "/api/startup/unregister":
            self._send_json(unregister_startup())
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
            # The 終了 button: optionally snapshot memory (blocks a few seconds
            # while the CLI runs) before stopping. The client awaits this reply,
            # so it can show "記憶を保存して終了中…" during the wait.
            remembered = finalize_memory_if_enabled()
            self._send_json({"ok": True, "remembered": remembered})
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


def _lan_ip():
    """Best-effort LAN IPv4 of this machine (for the phone to connect to).

    Uses a dummy UDP connect so no packets are actually sent; falls back to the
    hostname lookup, then to a placeholder if both fail."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "<このPCのIPアドレス>"


def main():
    global _SERVER
    url = f"http://{HOST}:{PORT}/"
    # Reject a duplicate launch loudly instead of silently co-binding the port
    # (Windows SO_REUSEADDR would otherwise let two servers share port 8765).
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    except OSError:
        print(f"ポート {PORT} は使用中です。ToDoChat は既に起動している可能性があります。")
        print(f"ウィンドウが見当たらない場合は {url} を開いてください。")
        open_app_window(url)
        return
    _SERVER = server
    print(f"ToDoChat running at {url}")
    print(f"  CLI     : {CLI}")
    print(f"  current : {CONFIG['current']}")
    if BIND_HOST not in ("127.0.0.1", "localhost"):
        lan_ip = _lan_ip()
        print(f"  LAN     : 同一ネットワークのスマホ等からは http://{lan_ip}:{PORT}/ で接続できます")
        print("  ⚠ 認証未実装のため、信頼できるネットワーク（自宅Wi-Fi/VPN）でのみ公開してください。")
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
