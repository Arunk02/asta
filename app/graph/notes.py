"""Job notes — what a task has established, kept outside any one context window.

A brain's session is where a task's knowledge lives, and a session is fragile:
a usage limit ends it, a switch to another brain cannot inherit it, a
compaction summarises it. The old engine patched each case separately
(`handoff.md`, `todos.md`, `task_spec`, a recap). This is one mechanism for all
of them: short dated lines — decisions, facts found, what is left — written as
the job goes and handed to every FRESH leg, so a new session starts from what
the old one knew instead of from the ticket.

Capped, because notes are a summary, not a transcript: a note list that grows
without bound is the token bleed the session rotation was built to stop.
"""

from __future__ import annotations

import json
import time

from app import store

MAX_NOTES = 12
MAX_CHARS = 1500


def _key(task_id: int) -> str:
    return f"task_notes:{task_id}"


def all_notes(task_id: int) -> list[str]:
    try:
        rows = json.loads(store.kv_get(_key(task_id)) or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, str)]


def add(task_id: int, text: str) -> None:
    line = " ".join((text or "").split())[:300]
    if not line:
        return
    rows = all_notes(task_id)
    if any(r.split(" · ", 1)[-1] == line for r in rows):
        return
    stamp = time.strftime("%d %b %H:%M")
    rows.append(f"{stamp} · {line}")
    store.kv_set(_key(task_id), json.dumps(rows[-MAX_NOTES:]))


def block(task_id: int) -> str:
    """The notes for a fresh leg's prompt, newest last, within the cap — or ''."""
    rows = all_notes(task_id)
    if not rows:
        return ""
    out: list[str] = []
    size = 0
    for line in reversed(rows):
        if size + len(line) > MAX_CHARS:
            break
        out.append(line)
        size += len(line)
    body = "\n".join(f"- {line}" for line in reversed(out))
    return ("\n\n[Job notes — what earlier runs of THIS task established. Trust them; "
            f"do not re-derive them]\n{body}")
