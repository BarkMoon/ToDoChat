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
    if name in _SAFE_COMMANDS:
        return not any(_looks_like_output_flag(a) for a in tokens[1:])
    return False


def is_safe_command(command):
    """True only when `command` is certain to be read-only (safe to auto-run)."""
    if not command or not isinstance(command, str):
        return False
    if any(bad in command for bad in _FORBIDDEN_SUBSTRINGS):
        return False
    # Reject a background '&' (a stray single '&', not part of '&&').
    if "&" in command.replace("&&", ""):
        return False
    segments = [s.strip() for s in _SEPARATORS.split(command) if s.strip()]
    if not segments:
        return False
    return all(_segment_safe(s) for s in segments)
