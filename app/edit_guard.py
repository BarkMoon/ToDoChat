#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ToDoChat edit/confirm-mode safety guard (Claude Code PreToolUse hook).

Registered when the UI is in edit or confirm mode. It reads a PreToolUse hook
payload from stdin and decides, per tool call, whether to allow or deny it:

  * Read / Glob / Grep / LS / NotebookRead  -> allow (read-only)
  * Edit / Write / MultiEdit / NotebookEdit -> allow ONLY if the target path is
    inside the working folder; deny anything that escapes it (absolute paths,
    ".." traversal, other drives)
  * Bash (shell / app execution):
      - edit mode    -> deny (shell is off)
      - confirm mode -> ask the server (/api/hook/permission), which surfaces an
        approval prompt to the user and blocks until they answer; allow/deny per
        the user's choice. This is how each command gets individual approval.
  * anything else                           -> deny (fail closed)

This is the real safety boundary. Do NOT rely on --allowedTools or
--permission-mode for confinement: in headless (-p) mode an allow-listed tool
runs unconditionally and is NOT confined to the working folder. The deny
decisions emitted here are authoritative and override --allowedTools (verified).

Env from the server: TODOCHAT_PROJECT_ROOT (folder writes must stay inside),
TODOCHAT_MODE (edit|confirm), TODOCHAT_RUN_ID + TODOCHAT_PERM_URL (how to ask
for per-command approval).
"""
import json
import os
import sys
import urllib.request

READ_ONLY = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
FILE_WRITE = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path", "path")
ASK_TIMEOUT = 330   # a bit longer than the server's own wait, then fail closed


def decide(decision, reason):
    """Emit the hook decision as UTF-8 JSON and exit (0 = handled)."""
    payload = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,          # "allow" | "deny"
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False)
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.exit(0)


def norm(p):
    """Absolute, normalized, case-folded path. abspath() collapses '..' so
    traversal escapes are caught by the prefix check below."""
    return os.path.normcase(os.path.abspath(p))


def ask_user(data):
    """Confirm mode: ask the server to get the user's allow/deny for this Bash
    command. Blocks until the user answers. Fails closed (deny) on any error."""
    url = os.environ.get("TODOCHAT_PERM_URL")
    run_id = os.environ.get("TODOCHAT_RUN_ID")
    if not url or not run_id:
        decide("deny", "許可サーバーに接続できません（実行はブロックされました）。")
    body = json.dumps({
        "run_id": run_id,
        "tool": data.get("tool_name"),
        "tool_input": data.get("tool_input") or {},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=ASK_TIMEOUT) as resp:
            r = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        decide("deny", "許可の取得に失敗しました（ブロック）: %s" % e)
    if r.get("decision") == "allow":
        decide("allow", "ユーザーが実行を許可しました。")
    decide("deny", "ユーザーが実行を拒否しました。")


def main():
    try:
        # Read stdin as raw bytes and decode UTF-8 explicitly. The CLI writes the
        # hook payload as UTF-8, but sys.stdin uses the console/locale encoding
        # (cp932 on Japanese Windows), so json.load(sys.stdin) mangles multibyte
        # text -- e.g. a Japanese `git commit -m "..."` shows up garbled in the
        # approval card. Reading the bytes and decoding UTF-8 keeps it intact.
        raw = sys.stdin.buffer.read()
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        decide("deny", "フック入力の解析に失敗しました。")
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    mode = os.environ.get("TODOCHAT_MODE", "edit")
    root = os.environ.get("TODOCHAT_PROJECT_ROOT") or data.get("cwd") or os.getcwd()
    root_n = norm(root)

    if tool in READ_ONLY:
        decide("allow", "read-only tool")

    if tool == "Bash" or tool.startswith("Bash") or tool == "KillShell":
        if mode == "confirm":
            ask_user(data)   # blocks; emits allow/deny per the user's choice
        decide("deny", "編集モードではシェル実行(Bash)は無効です。"
                       "「実行（都度確認）」モードに切り替えてください。")

    if tool in FILE_WRITE:
        path = next((ti[k] for k in PATH_KEYS if ti.get(k)), None)
        if not path:
            decide("deny", "対象ファイルパスを特定できませんでした。")
        tn = norm(path)
        if tn == root_n or tn.startswith(root_n + os.sep):
            decide("allow", "作業フォルダ内のファイル編集")
        decide("deny", "作業フォルダ外への書き込みは禁止です: %s" % path)

    decide("deny", "未許可のツールです: %s" % tool)


if __name__ == "__main__":
    main()
