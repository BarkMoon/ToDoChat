#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ToDoChat safe-shell allowlist (single source of truth).

`is_safe_command(command)` returns True only for shell commands that inspect
state without changing any file, branch, or repository state -- e.g. `git log`,
`git status`, `head`, `echo`. Confirm mode uses this to AUTO-approve such
commands (the approval card is still shown for visibility, but the user does not
have to click). Anything this module is not certain about returns False and
falls back to the normal per-command manual approval -- so a missed command
costs one extra click, never an unsafe auto-run.

This is a security boundary, so the guiding rule throughout is: WHEN IN DOUBT,
RETURN FALSE. We never try to prove a command is dangerous; we only return True
when we can positively prove it is read-only.

Why the whole raw string is scanned before anything is allowed: a shell command
can smuggle an unsafe action into an otherwise-safe one via substitution
(`$(...)`, backticks), redirection (`git log > file` writes a file), subshells,
or chaining. We reject any command containing those constructs outright, then
split what remains on the pipe/and/or/semicolon separators and require EVERY
segment to be independently safe.

Harmless redirections are the one exception: `2>/dev/null`, `>/dev/null`,
`2>&1`, the Windows `NUL` equivalents, etc. discard or merge output and write no
real file, so they are stripped up front (see _SAFE_REDIR_RE) before the
`>`/`<` rejection -- otherwise silencing stderr would force an otherwise
read-only command to manual approval. Any redirection that is NOT one of these
provably-harmless forms still leaves a `>`/`<` behind and is rejected.
"""
import os
import re
import shlex
from pathlib import Path

# Shell constructs we refuse to reason about. Their presence anywhere in the
# command makes it non-auto-safe (it falls back to manual approval):
#   `  $(  ${   command / arithmetic substitution (can run anything)
#   >  <  >>    redirection (can create or overwrite files)
#   (  )  {  }  subshells / grouping / brace expansion
#   newline     multiple statements on separate lines
#   backslash   line continuation / escaping tricks
_FORBIDDEN_SUBSTRINGS = ("`", "$(", "${", ">", "<", "(", ")", "{", "}", "\n", "\r", "\\")

# Sub-command separators. We split on these and check each piece; a lone `&`
# (background execution) is rejected separately below.
_SEPARATORS = re.compile(r"&&|\|\||;|\|")

# Provably-harmless redirections, stripped BEFORE the `>`/`<` rejection above.
# They discard output to the null device or merge one stream into another, so no
# real file is written:
#   [n]>/dev/null  [n]>>/dev/null  &>/dev/null   -> discard (Unix)
#   [n]>NUL        [n]>>NUL                       -> discard (Windows)
#   [n]>&[m]                                      -> merge streams (e.g. 2>&1)
# The stream-merge form REQUIRES a trailing digit ([m]); `>&word` where word is a
# filename would otherwise write that file, so it is intentionally NOT matched
# and its `>` still triggers rejection.
_SAFE_REDIR_RE = re.compile(
    r"[0-9&]?>>?\s*/dev/null\b"
    r"|[0-9]?>>?\s*(?:NUL|nul)\b"
    r"|[0-9]?>&[0-9]"
)

# The command names / subcommands / flags themselves live in an easy-to-edit,
# one-per-line data file (safe_commands.txt) so the allowlist can be reviewed
# and extended without touching this logic. See that file's header for format.
_ALLOWLIST_FILE = Path(__file__).resolve().parent / "safe_commands.txt"
_LISTING_PREFIX = "git-listing."


def _load_allowlist(path):
    """Parse safe_commands.txt into {section-name: [items]}.

    Fails CLOSED: if the file is missing or unreadable, every section comes back
    empty, so is_safe_command() auto-approves nothing and all commands fall back
    to manual confirmation -- the safe direction on error."""
    sections = {}
    current = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return sections
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


_ALLOW = _load_allowlist(_ALLOWLIST_FILE)

# git subcommands that only READ -- they cannot modify a file, the index, the
# working tree, or any ref, regardless of the options given (output-to-file
# flags are rejected separately in _git_safe).
_GIT_READONLY = set(_ALLOW.get("git-readonly", ()))

# git subcommands that are read-only ONLY when listing (they can also mutate,
# e.g. `git branch -d` / `git tag v1` / `git remote add`), mapped to the set of
# listing-only flags that keep them read-only. Built from every [git-listing.*]
# section in the data file.
_GIT_LISTING = {
    name[len(_LISTING_PREFIX):]: set(items)
    for name, items in _ALLOW.items() if name.startswith(_LISTING_PREFIX)
}

# Non-git commands that read/inspect and write nothing to disk on their own.
_SAFE_COMMANDS = set(_ALLOW.get("commands", ()))

# `find` action primaries that write a file, delete, or run other programs. find
# is read-only (its default action is -print) UNLESS one of these appears, and
# this is find's COMPLETE mutating surface (the primary set is closed), so
# checking for their absence is a positive proof of read-only, not a guess.
# Deliberately hardcoded, not loaded from safe_commands.txt: this is a DENYLIST,
# where an empty/missing set must mean "unsafe" -- the opposite of the allowlist
# sections' fail-closed behavior, which would make a load failure auto-approve
# `find -delete`. Keeping it in code guarantees it can never silently empty out.
_FIND_UNSAFE = frozenset({
    "-exec", "-execdir", "-ok", "-okdir",
    "-delete", "-fprint", "-fprint0", "-fprintf", "-fls",
})

# Options that make an otherwise read-only command write to a file. Any argument
# matching one of these disqualifies the command from auto-approval.
_OUTPUT_FLAGS = ("-o", "--output")


def _looks_like_output_flag(arg):
    return arg == "-O" or arg.startswith(_OUTPUT_FLAGS)


def _prog_name(token):
    """Bare program name for matching: strips any directory (/usr/bin/echo) and
    a trailing .exe (echo.exe on Windows)."""
    name = os.path.basename(token)
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _git_safe(args):
    """True if a `git <args>` invocation only reads repository state."""
    i = 0
    # Only benign global flags may precede the subcommand. Anything else --
    # notably `-c key=val` / `-C dir` / `--git-dir` -- can redirect git or run
    # arbitrary code via config (e.g. -c core.pager=...), so we bail to manual.
    while i < len(args) and args[i] in ("--no-pager", "--paginate"):
        i += 1
    if i >= len(args):
        return False                       # bare `git` -- nothing to run
    sub, rest = args[i], args[i + 1:]
    if any(_looks_like_output_flag(a) for a in rest):
        return False                       # writes results to a file
    if sub in _GIT_READONLY:
        return True
    if sub in _GIT_LISTING:
        return all(a in _GIT_LISTING[sub] for a in rest)
    return False


def _find_safe(args):
    """True if a `find ...` invocation only searches/lists (no action primary
    that writes a file, deletes, or executes another program)."""
    return all(a not in _FIND_UNSAFE for a in args)


def _segment_safe(segment):
    """True if one separator-delimited piece of the command is read-only."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False                       # unbalanced quotes etc. -> not sure
    if not tokens:
        return False
    prog = tokens[0]
    if "=" in prog:
        return False                       # VAR=val prefix -> could set env for a command
    name = _prog_name(prog)
    if name == "git":
        return _git_safe(tokens[1:])
    if name == "find":
        return _find_safe(tokens[1:])
    if name in _SAFE_COMMANDS:
        return not any(_looks_like_output_flag(a) for a in tokens[1:])
    return False


def is_safe_command(command):
    """True only when `command` is certain to be read-only (safe to auto-run)."""
    if not command or not isinstance(command, str):
        return False
    # Drop provably-harmless redirections first (2>/dev/null, 2>&1, >NUL, ...)
    # so they don't trip the `>`/`<` rejection. Anything else stays put; a real
    # file-writing redirection still leaves a `>` behind and is rejected below.
    command = _SAFE_REDIR_RE.sub(" ", command)
    if any(bad in command for bad in _FORBIDDEN_SUBSTRINGS):
        return False
    # Reject a background '&' (a stray single '&', not part of '&&').
    if "&" in command.replace("&&", ""):
        return False
    segments = [s.strip() for s in _SEPARATORS.split(command) if s.strip()]
    if not segments:
        return False
    return all(_segment_safe(s) for s in segments)
