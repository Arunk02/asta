"""One live conversation per person per channel — and what is left once it ends.

His design, 29 Sep, after a day of Asta judging every message alone:

    single thread per person, once the convo resolved automatically dissolve the
    thread context and make it free, to track convo back to back

A message used to arrive with no idea what came before it or what it closed.
Navya's "Thank you" after his own reaction was pushed in red; Yogesh's three
lines about one defect were three red pushes; "please ping when free" was news.
Each was one message judged on its own.

So the unit is a conversation now:

  * ``conv_threads`` holds the LIVE one: its status, what they need, a rolling
    summary, the entities it is about (PR links, tickets, booking ids). One row
    per ``<channel>:<counterpart>`` — a 1:1 is keyed by the person, a group by
    the chat. The channel is part of the key so WhatsApp, Telegram, Outlook or
    anything else can use the same model.
  * When it is resolved and quiet for DISSOLVE_SECONDS it DISSOLVES: the summary
    moves to ``conv_episodes`` and the live row is deleted. Its context is freed,
    as he asked; nothing it learned is lost.
  * When they come back — tomorrow, or in three days, about the same PR — the
    episodes that share an entity with the new message come back first, then
    those that share words, so the conversation picks up where it left off.

State only. Deciding what a conversation IS is `understand`; acting on it is the
sweep. This module never sends, pushes or calls a model.
"""

from __future__ import annotations

import json
import os
import re
import time

from . import store

#: A resolved conversation stays reopenable this long — "thanks" … "oh, one more
#: thing" twenty minutes later is still the same conversation.
DISSOLVE_SECONDS = float(os.environ.get("ASTA_THREAD_DISSOLVE_MINUTES", "120") or 120) * 60

#: An open conversation nobody has touched in a day is abandoned. It dissolves
#: too, so no thread lives for ever and no stale "open" skews the next one.
STALE_SECONDS = 24 * 3600

#: How far back a returning conversation can reach. A month-old episode is
#: history, not context.
PAST_DAYS = 30


def enabled() -> bool:
    return os.environ.get("ASTA_THREADS", "").strip().lower() in ("1", "true", "yes", "on")


def tid(channel: str, counterpart: str) -> str:
    return f"{(channel or '').strip().lower()}:{(counterpart or '').strip()}"


def _row(r) -> dict:
    d = dict(r)
    try:
        d["entities"] = json.loads(d.get("entities") or "[]")
    except ValueError:
        d["entities"] = []
    return d


def get(thread_id: str) -> dict | None:
    with store._connect() as conn:
        r = conn.execute("SELECT * FROM conv_threads WHERE id=?", (thread_id,)).fetchone()
    return _row(r) if r else None


def open(channel: str, counterpart: str, chat: str = "", now: float | None = None) -> dict:
    """The live conversation with this person, created or reopened.

    A closed one that has not dissolved yet is REOPENED rather than replaced:
    "thanks" followed by "oh, one more thing" is one conversation.
    """
    now = time.time() if now is None else now
    key = tid(channel, counterpart)
    t = get(key)
    with store._connect() as conn:
        if t is None:
            conn.execute(
                "INSERT INTO conv_threads (id, channel, counterpart, chat, status, "
                "opened_at, last_activity) VALUES (?,?,?,?,?,?,?)",
                (key, channel.lower(), counterpart.strip(), chat or counterpart,
                 "open", now, now))
            _record(key, "opened", "")
        elif t["status"] == "closed":
            conn.execute("UPDATE conv_threads SET status='open', closed_at=NULL, "
                         "checked_in=0, last_activity=? WHERE id=?", (now, key))
            _record(key, "reopened", "they wrote again before it dissolved")
        else:
            conn.execute("UPDATE conv_threads SET last_activity=? WHERE id=?", (now, key))
    return get(key) or {}


def is_new(t: dict, now: float | None = None) -> bool:
    """Opened in this sweep — the moment to reach back for its past."""
    now = time.time() if now is None else now
    return abs(float(t.get("opened_at") or 0) - float(t.get("last_activity") or 0)) < 1.0


def update(thread_id: str, **fields) -> None:
    if not fields:
        return
    if "entities" in fields:
        fields["entities"] = json.dumps(sorted(set(fields["entities"] or []))[:30])
    keys = ", ".join(f"{k}=?" for k in fields)
    with store._connect() as conn:
        conn.execute(f"UPDATE conv_threads SET {keys} WHERE id=?",
                     (*fields.values(), thread_id))


def close(thread_id: str, why: str = "", now: float | None = None) -> None:
    now = time.time() if now is None else now
    with store._connect() as conn:
        conn.execute("UPDATE conv_threads SET status='closed', closed_at=? WHERE id=?",
                     (now, thread_id))
    _record(thread_id, "closed", why)


def remember_episode(channel: str, counterpart: str, *, need: str = "", summary: str = "",
                     entities: list | None = None, outcome: str = "",
                     opened_at: float | None = None, closed_at: float | None = None) -> int:
    closed_at = time.time() if closed_at is None else closed_at
    with store._connect() as conn:
        cur = conn.execute(
            "INSERT INTO conv_episodes (channel, counterpart, need, summary, entities, "
            "outcome, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?)",
            (channel.lower(), counterpart.strip(), need or "", summary or "",
             json.dumps(sorted(set(entities or []))[:30]), outcome,
             opened_at if opened_at is not None else closed_at, closed_at))
        return int(cur.lastrowid)


def dissolve_due(now: float | None = None) -> list[dict]:
    """Free every conversation that is over, keeping what it was about.

    Closed and quiet for DISSOLVE_SECONDS, or untouched for STALE_SECONDS in any
    state. Returns what dissolved.
    """
    now = time.time() if now is None else now
    with store._connect() as conn:
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM conv_threads WHERE "
            "(status='closed' AND closed_at IS NOT NULL AND ? - closed_at > ?) "
            "OR (? - last_activity > ?)",
            (now, DISSOLVE_SECONDS, now, STALE_SECONDS)).fetchall()]
    for t in rows:
        if t.get("summary") or t.get("need"):
            remember_episode(t["channel"], t["counterpart"], need=t["need"],
                             summary=t["summary"], entities=t["entities"],
                             outcome=t["status"], opened_at=t["opened_at"],
                             closed_at=t.get("closed_at") or t["last_activity"])
        with store._connect() as conn:
            conn.execute("DELETE FROM conv_threads WHERE id=?", (t["id"],))
        _record(t["id"], "dissolved", (t.get("summary") or "")[:160])
    return rows


# --- what links a conversation to its past ----------------------------------------------

_PR = re.compile(r"github\.com/[\w.-]+/[\w.-]+/pull/\d+", re.I)
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,14}-\d+\b")
_INCIDENT = re.compile(r"\bINC\d{5,}\b", re.I)
_NUMBER = re.compile(r"\b\d{6,12}\b")


def entities_in(text: str) -> list[str]:
    """The things a conversation is ABOUT: PRs, tickets, incidents, long ids.

    These, not words, are what make "the same work" recognisable three days on —
    "can you look at 1251 again" shares almost no words with the first ask and
    shares the one thing that matters.
    """
    text = text or ""
    found = [m.group(0).lower() for m in _PR.finditer(text)]
    found += [m.group(0).upper() for m in _INCIDENT.finditer(text)]
    found += [m.group(0) for m in _TICKET.finditer(text)
              if not m.group(0).upper().startswith("INC")]
    found += [m.group(0) for m in _NUMBER.finditer(text)]
    return sorted(set(found))


def _pr_number(entity: str) -> str:
    m = re.search(r"/pull/(\d+)$", entity)
    return m.group(1) if m else ""


def past(counterpart: str, text: str, entities: list | None = None,
         now: float | None = None, limit: int = 2) -> list[dict]:
    """This person's earlier conversations that the new message picks back up.

    Ranked by shared entities first — the same PR or ticket is the strongest
    possible signal — and then by shared words. Nothing older than PAST_DAYS.
    """
    from . import knowledge
    now = time.time() if now is None else now
    ents = set(entities or entities_in(text))
    numbers = {_pr_number(e) for e in ents} | {e for e in ents if e.isdigit()}
    numbers.discard("")
    words = {w for w in knowledge.terms(text) if len(w) > 3}
    with store._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM conv_episodes WHERE counterpart=? AND closed_at>=? "
            "ORDER BY closed_at DESC LIMIT 50",
            (counterpart.strip(), now - PAST_DAYS * 86400)).fetchall()]
    scored = []
    for r in rows:
        try:
            theirs = set(json.loads(r.get("entities") or "[]"))
        except ValueError:
            theirs = set()
        body = f"{r.get('need', '')} {r.get('summary', '')}".lower()
        shared = len(ents & theirs)
        # "PR 1251" in a message and ".../pull/1251" in an episode are the same PR.
        shared += sum(1 for n in numbers if n and (n in body or any(
            _pr_number(e) == n for e in theirs)))
        overlap = sum(1 for w in words if w[:5] in body)
        score = shared * 10 + overlap
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda s: (-s[0], -float(s[1]["closed_at"])))
    return [r for _, r in scored[:limit]]


def _record(thread_id: str, event: str, detail: str) -> None:
    """The conversation's timeline — "why did you push Navya's thanks?" is
    answered from here, not reconstructed from memory."""
    import contextlib
    with contextlib.suppress(Exception):
        store.record_outcome("thread", event, subject=thread_id[:80], detail=detail[:200])
