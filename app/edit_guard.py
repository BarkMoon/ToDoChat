#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ToDoChat edit-mode safety guard (Claude Code PreToolUse hook).

Registered only when the UI's edit mode is on. It reads a PreToolUse hook
payload from stdin and decides, per tool call, whether to allow or deny it:

  * Read / Glob / Grep / LS / NotebookRead  -> allow (read-only)
  * Edit / Write / MultiEdit / NotebookEdit -> allow ONLY if the target path is
    inside the working folder; deny anything that escapes it (absolute paths,
    ".." traversal, other drives)
  * Bash / any shell tool                   -> deny (shell execution is off in v1)
  * anything else                           -> deny (fail closed)

This is the real safety boundary for edit mode. Do NOT rely on --allowedTools or
--permission-mode for confinement: in headless (-p) mode an allow-listed tool
runs unconditionally and is NOT confined to the working folder. The deny
decisions emitted here are authoritative and cannot be overridden by the model.

The working folder is taken from the TODOCHAT_PROJECT_ROOT env var that the
server sets when spawning claude, falling back to the hook payload's cwd.
"""
import json
import os
import sys

READ_ONLY = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
FILE_WRITE = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path", "path")


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


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        decide("deny", "フック入力の解析に失敗しました。")
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    root = os.environ.get("TODOCHAT_PROJECT_ROOT") or data.get("cwd") or os.getcwd()
    root_n = norm(root)

    if tool in READ_ONLY:
        decide("allow", "read-only tool")

    if tool == "Bash" or tool.startswith("Bash") or tool == "KillShell":
        decide("deny", "編集モードではシェル実行(Bash)は無効です。"
                       "ファイル編集(Edit/Write)のみ許可されています。")

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
