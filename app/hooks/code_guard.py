"""PreToolUse guard for Asta's code legs: the rules that cost tokens, enforced.

Wired by app/claude_cli.py (`--settings` on a code leg). Reads Claude Code's hook
JSON on stdin; prints a deny decision or nothing. Standard library only — it
runs once per tool call.

Why (task 744, 25 Nov — 216k context, 16M cache reads, the spend limit hit):
- 8 Reads with no range dumped whole files (95k chars) into a session that
  re-reads its context every turn, though the pipeline said "anchored reads only".
- 22 edits went through `python3` heredocs, not Edit: the planning leg denies
  Edit but not Bash, so the planner wrote files, and the habit carried into the
  build. A prose rule is a wish; this is the gate.

Mode (argv[1]): "plan" — nothing may be written; "build" — files change through
Edit/Write, never through the shell.
"""
from __future__ import annotations

import json
import os
import re
import sys

#: A file longer than this is read with offset/limit around the line needed.
READ_MAX_LINES = int(os.environ.get("ASTA_READ_MAX_LINES", "400"))

_TMP = ("/tmp/", "/private/tmp/", "/var/folders/", "/dev/null", "/dev/stderr", "/dev/stdout")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*(?:\n|$)", re.S)
_REDIRECT = re.compile(r"(?<![<>&\d])(?:\d)?>>?\s*(?!&)([^\s;&|()<>]+)")
_PY_WRITE = re.compile(r"\.write_text\(|\.write_bytes\(|open\([^)]*['\"][wa]b?\+?['\"]|\bshutil\.copy")


def _tmp(path: str) -> bool:
    p = path.strip("'\"")
    tmpdir = os.environ.get("TMPDIR", "")
    return (p.startswith(_TMP) or p.startswith("$TMPDIR") or p.startswith("${TMPDIR")
            or (bool(tmpdir) and p.startswith(tmpdir)) or p in ("-", "&1", "&2"))


def _lessons(path: str) -> bool:
    return path.strip("'\"").endswith("lessons.md")


def shell_write(command: str) -> str:
    """What in this command writes a file outside a temp dir, or ''."""
    bodies = [m.group(3) for m in _HEREDOC.finditer(command)]
    raw = _HEREDOC.sub("<<HEREDOC\n", command)
    # Quoted text is data, not shell: `git commit -m "x > y"` writes no file.
    outer = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "Q", raw)
    if re.search(r"\bsed\s+(?:-[a-zA-Z]*\s+)*-i\b|\bsed\s+-i", outer):
        return "sed -i"
    if re.search(r"\bperl\s+-[a-zA-Z]*i", outer):
        return "perl -i"
    for m in _REDIRECT.finditer(outer):
        target = m.group(1)
        if not (_tmp(target) or _lessons(target)):
            return f"redirect into {target}"
    for m in re.finditer(r"\btee\s+(?:-a\s+)?([^\s;&|]+)", outer):
        if not (_tmp(m.group(1)) or _lessons(m.group(1))):
            return f"tee into {m.group(1)}"
    for text in bodies + [raw]:
        if re.search(r"\b(?:python3?|node|ruby)\b", raw) and _PY_WRITE.search(text):
            paths = re.findall(r"['\"]([^'\"\s(),]+)['\"]", text)
            if not paths or not all(_tmp(p) for p in paths if "/" in p or "." in p):
                return "a script writing files"
    return ""


def _count_lines(path: str) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def decide(event: dict, mode: str) -> str:
    """The reason to deny this tool call, or '' to let it run."""
    tool = event.get("tool_name", "")
    args = event.get("tool_input") or {}
    if tool == "Read" and not args.get("limit"):
        path = args.get("file_path", "")
        n = _count_lines(path)
        if n > READ_MAX_LINES:
            return (f"{os.path.basename(path)} has {n} lines. Read it anchored: offset and "
                    f"limit around the line you need (about 80 lines), or grep -n first. A "
                    f"whole file stays in context and is re-read on every later turn.")
    if tool == "Bash":
        what = shell_write(args.get("command", ""))
        if what:
            if mode == "plan":
                return (f"Planning leg: nothing may change ({what}). Read and plan only; the "
                        f"build runs after his approval.")
            return (f"Blocked: {what}. Change files with the Edit tool (or Write for a new "
                    f"file) so the change is exact and reviewable; redirect build output to "
                    f"/tmp only.")
    return ""


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "build"
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    why = decide(event, mode)
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": why}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
