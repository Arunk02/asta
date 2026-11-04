"""Action items from a meeting recap, kept with their owners until they are done.

meeting_recap wrote action items into a message and pinged him when one was his,
and that was all: nothing remembered them, and summarising the same transcript
again pinged him again. Here each item is a row with its meeting, owner and
status. The same transcript records nothing twice and tells him nothing twice.

Recording is all this does. Nothing chases a colleague: an item that is theirs is
theirs, and reminding them would be speaking for him.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import store

KEY = "meeting_actions"
KEEP = 200
#: "- Priya: send the numbers". A longer prefix is a sentence, not a name.
#: A bullet marker is followed by a space, so "**Open questions**" is a heading.
_MARK = r"^\s*(?:[-•]|\*(?!\*)|\d+[.)])\s+"
_OWNED = re.compile(_MARK + r"(?:\*\*)?([^:*\n]{1,40}?)(?:\*\*)?\s*:\s*(.+)$")
_BULLET = re.compile(_MARK + r"(.+)$")
_NONE = re.compile(r"^\(?(none|n/?a|nothing|no action items?)\)?\.?$", re.I)


def _load() -> list[dict]:
    try:
        rows = json.loads(store.kv_get(KEY) or "[]")
    except (ValueError, TypeError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def _save(rows: list[dict]) -> None:
    """Bounded by dropping the oldest finished items; an open one is never forgotten."""
    spare = len(rows) - KEEP
    if spare > 0:
        drop = {id(r) for r in rows if r.get("status") != "open"}
        rows = [r for r in rows if not (id(r) in drop and (spare := spare - 1) >= 0)]
    store.kv_set(KEY, json.dumps(rows))


def parse(recap: str) -> list[dict]:
    """The bullets under the recap's **Action items** heading, as (owner, text)."""
    items, inside = [], False
    for line in (recap or "").splitlines():
        heading = re.sub(r"[*#_:]", "", line).strip().lower()
        if heading in ("action items", "actions", "action points"):
            inside = True
            continue
        if inside and (line.lstrip().startswith(("**", "#")) and not _BULLET.match(line)):
            break                                     # the next section
        if not inside:
            continue
        owned = _OWNED.match(line)
        if owned and len(owned.group(1).split()) <= 4:
            owner, text = owned.group(1).strip(), owned.group(2).strip()
        else:
            bullet = _BULLET.match(line)
            if not bullet:
                continue
            owner, text = "", bullet.group(1).strip()
        if text and not _NONE.match(text):
            items.append({"owner": owner or "unassigned", "text": text[:300],
                          "his": owner.strip().upper() == "ARUN"})
    return items


def record(recap: str, title: str, transcript: str) -> tuple[list[dict], list[dict]]:
    """(this meeting's rows, the rows of his that were not yet told to him)."""
    source = hashlib.sha256((transcript or "").strip().encode()).hexdigest()[:16]
    rows = _load()
    mine = [r for r in rows if r.get("source") == source]
    if not mine:
        next_id = max([r["id"] for r in rows], default=0) + 1
        now = time.time()
        for n, item in enumerate(parse(recap)):
            mine.append({"id": next_id + n, "meeting": (title or "").strip()[:120],
                         "source": source, "created_at": now, "status": "open",
                         "told": False, **item})
        rows += mine
    untold = [r for r in mine if r["his"] and not r["told"]]
    if mine:
        _save(rows)
    return mine, untold


def mark_told(ids: list[int]) -> None:
    rows = _load()
    for r in rows:
        if r["id"] in ids:
            r["told"] = True
    _save(rows)


def list_open(owner: str = "") -> list[dict]:
    want = owner.strip().lower()
    return [r for r in _load() if r.get("status") == "open"
            and (not want or r["owner"].lower() == want or (want in ("me", "arun") and r["his"]))]


def done(item_id: int) -> str:
    rows = _load()
    for r in rows:
        if r["id"] == int(item_id) and r.get("status") == "open":
            r["status"], r["done_at"] = "done", time.time()
            _save(rows)
            return f"Done: #{r['id']} {r['owner']}: {r['text'][:80]}"
    return f"No open meeting action #{item_id}."
