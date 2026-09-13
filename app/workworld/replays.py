"""Replays of his own corrections — the P4 bar: every standing instruction he
gave in the last 60 days, played back against the Asta of today.

Mined from his real messages (read-only), compiled by the same instruction
compiler the front desk uses, and written beside his database — data/, which
git ignores — because each replay quotes what he actually said. The repo keeps
synthetic equivalents in tests/workworld/rules.yaml.
"""

from __future__ import annotations

import sqlite3
import time

from app import instructions, store
from app.policy import Rule


def his_messages(days: int = 60) -> list[tuple[str, float]]:
    from app import scorecard
    db = store.DB_PATH
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT content, created_at FROM ui_messages WHERE role='user' "
                           "AND created_at >= ? ORDER BY created_at",
                           (time.time() - days * 86400,)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        w = scorecard.his_words(r["content"])
        if w:
            out.append((w, r["created_at"]))
    return out


def from_history(days: int = 60) -> list[str]:
    """Compile each standing instruction into a replay; returns one line per replay."""
    seen: set[tuple] = set()
    lines = []
    for i, (text, at) in enumerate(his_messages(days), start=1):
        cand = instructions.compile(text)
        if cand is None or cand.kind == "note" and not text.lstrip().lower().startswith(
                ("always", "never", "don't", "dont", "do not", "from now on", "going forward")):
            continue
        key = (cand.kind, cand.act, cand.target.lower(), cand.value)
        if key in seen:
            continue
        seen.add(key)
        said_on = time.strftime("%Y-%m-%d", time.localtime(at))
        rule = Rule(id=i, kind=cand.kind, act=cand.act, target=cand.target,
                    value=cand.value, unless_asked=cand.unless_asked, words=cand.words)
        instructions.write_replay(rule, said_on)
        lines.append(f"{said_on} · {rule.render()}")
    return lines
