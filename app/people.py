"""People and systems — who owns what, how a person works, what a repo does.

Kept apart from the memory notes on purpose. A note is prose a brain may or may
not read; a fact here has a subject, a source and a date, so a context pack can
pull exactly the facts about the people and systems a job touches — and a fact
from March can be recognised as one from March.

Written by the brains (`note_fact`) and read by app/context_pack.py.
"""

from __future__ import annotations

import re
import time

from . import store

KINDS = ("person", "repo", "service", "env", "ticket", "fact")


def add(subject: str, fact: str, kind: str = "fact", source: str = "") -> int:
    subject, fact = " ".join((subject or "").split())[:80], " ".join((fact or "").split())[:400]
    if not subject or not fact:
        raise ValueError("a fact needs a subject and the fact itself")
    kind = kind if kind in KINDS else "fact"
    with store._connect() as conn:
        same = conn.execute("SELECT id FROM people_systems WHERE lower(subject)=lower(?) "
                            "AND lower(fact)=lower(?)", (subject, fact)).fetchone()
        if same:
            return same["id"]
        cur = conn.execute("INSERT INTO people_systems (subject, kind, fact, source, created_at) "
                           "VALUES (?,?,?,?,?)", (subject, kind, fact, source[:200], time.time()))
        return cur.lastrowid


def facts(subject: str = "", limit: int = 20) -> list[dict]:
    with store._connect() as conn:
        if subject:
            rows = conn.execute("SELECT * FROM people_systems WHERE lower(subject)=lower(?) "
                                "ORDER BY id DESC LIMIT ?", (subject, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM people_systems ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
    return [dict(r) for r in rows]


def about(text: str, limit: int = 8) -> list[dict]:
    """Facts whose subject is named in `text` — the people and systems a job touches."""
    t = (text or "").lower()
    if not t:
        return []
    with store._connect() as conn:
        subjects = [r["subject"] for r in conn.execute(
            "SELECT DISTINCT subject FROM people_systems").fetchall()]
    hit = [s for s in subjects if re.search(r"\b" + re.escape(s.lower()) + r"\b", t)]
    out: list[dict] = []
    for s in hit:
        out += facts(s, limit)
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:limit]


def line(r: dict) -> str:
    when = time.strftime("%d %b", time.localtime(r["created_at"]))
    src = f", {r['source']}" if r.get("source") else ""
    return f"{r['subject']}: {r['fact']} ({when}{src})"
