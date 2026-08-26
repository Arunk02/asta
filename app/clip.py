"""Cutting text for a phone without cutting it mid-word.

A message that stops at `Preparing worktree (c` is worse than a shorter one. It
reads as a crash rather than a trim, there is no way to tell which it was, and
nothing says whether the missing part mattered. Arun's words: "message getting
truncked in btw no proper conclusion".

The cause was hard character slices — `msg[:160]`, `text[:200]` — scattered
across the places that prepare something for him to read. Each was right about
needing a bound and wrong about where to put it. So the bound lives here, once,
and it ends on a boundary and SAYS it cut.

Deliberately dependency-free: the sites that need it run at every layer, and a
helper that drags imports behind it ends up copied instead of called.
"""

from __future__ import annotations

import re

#: Lines that say what actually went wrong. Command-line tools narrate their
#: progress first and fail last, so the first 160 characters of a failure are
#: usually the narration — which is exactly how a git error reached his phone as
#: "Preparing worktree (c" with the reason cut off the end.
_PROBLEM = re.compile(r"^\s*(fatal|error|ERROR|FAILED|Exception|Caused by)\b", re.M)


def clip(text: str, limit: int, marker: str = "…") -> str:
    """At most `limit` characters, ending on a boundary, marked when it cut.

    Prefers a line break, then a word break, and falls back to a hard cut only
    for a single unbroken token longer than the limit — where there is no
    boundary to find and the alternative is showing nothing.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    room = max(1, limit - len(marker))
    head = text[:room]
    for boundary in ("\n", " "):
        cut = head.rfind(boundary)
        # Only honour a boundary that leaves most of the budget used; a break at
        # character three would technically be a word boundary and would throw
        # away everything he asked for.
        if cut >= room // 2:
            return head[:cut].rstrip() + marker
    return head.rstrip() + marker


def problem(output: str, limit: int = 200) -> str:
    """The line of command output that says what went wrong.

    `git worktree add` prints "Preparing worktree…" and *then* fails, so a
    leading slice keeps the progress and drops the reason. Falls back to the last
    non-empty line, which is where a tool that says nothing structured puts it.
    """
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    named = [ln for ln in lines if _PROBLEM.match(ln)]
    return clip(named[0] if named else lines[-1], limit)
