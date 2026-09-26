"""The experience ledger — what he decided, in a shape something can learn from.

Round 3, P12: *"Approvals, edits, rejections and corrections are logged but
never learned from, and confidence is never calibrated."*

Every day already produces the only signal that matters. He approved it as it
was, he changed it before it went, he said no, or he never answered. That signal
existed — in a kv counter in `authority`, in an outcome row, and for edits
nowhere at all — in three shapes, none of which anything could learn from.

One row per judgement, with the features of the thing judged, so a learner can
ask a question and get a number: *of the drafts Asta claimed 0.9 for, how many
went out untouched?*

Two decisions worth stating, because both are about what is NOT here.

  IGNORED IS NOT A REJECTION. He never looked. That says something about the
  interruption and nothing about whether the draft was right, so `rate()`
  leaves it out. A learner that conflated them would stop sending the things he
  would have approved, on the evidence that he was busy.

  THE EDIT IS A SHAPE, NEVER THE WORDS. "shorter, greeting removed" is what is
  stored. This repo is public and his colleagues' messages are not, and a
  learner does not need the sentence to learn that he cuts the greeting.

No confidence is recorded as no confidence — not as zero. Zero means "certain
this is wrong", and most judgements carry no claim at all; reading those as
failed predictions would make calibration a measure of how often Asta declines
to guess.
"""

from __future__ import annotations

import json
import time

#: What he can do with something Asta put in front of him.
VERDICTS = ("as_is", "amended", "rejected", "ignored")

#: Verdicts that say something about the CONTENT. "ignored" does not.
JUDGED = ("as_is", "amended", "rejected")

#: Confidence buckets for calibration. Coarse on purpose: with a few hundred
#: judgements, ten buckets would each hold noise.
BUCKETS = (0.6, 0.7, 0.8, 0.9)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experience (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  kind TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL,
  confidence REAL,
  edit TEXT NOT NULL DEFAULT '',
  features TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS experience_kind ON experience (kind, target);
"""

_GREETINGS = ("hi", "hey", "hello", "good morning", "good afternoon", "good evening")


def _shape_of_edit(before: str, after: str) -> str:
    """How he changed it, in words a learner can group — never the words he wrote."""
    a, b = (before or "").strip(), (after or "").strip()
    if not a or not b or a == b:
        return ""
    notes = []
    if len(b) < len(a) * 0.8:
        notes.append("shorter")
    elif len(b) > len(a) * 1.2:
        notes.append("longer")
    low_a, low_b = a.lower(), b.lower()
    if any(low_a.startswith(g) for g in _GREETINGS) and \
            not any(low_b.startswith(g) for g in _GREETINGS):
        notes.append("greeting removed")
    if a.count("?") > b.count("?"):
        notes.append("question dropped")
    elif b.count("?") > a.count("?"):
        notes.append("question added")
    if not notes:
        notes.append("reworded")
    return ", ".join(notes)


def record(kind: str, target: str, verdict: str, *, confidence: float | None = None,
           before: str = "", after: str = "", **features) -> int:
    """One judgement. Returns its id.

    `confidence` is what Asta CLAIMED before he answered, if it claimed
    anything — that is the number calibration exists to check.
    """
    from . import store
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r} — one of {', '.join(VERDICTS)}")
    with store._connect() as conn:
        conn.executescript(_SCHEMA)
        cur = conn.execute(
            "INSERT INTO experience (at, kind, target, verdict, confidence, edit, features) "
            "VALUES (?,?,?,?,?,?,?)",
            (time.time(), (kind or "").strip()[:40], (target or "").strip()[:120],
             verdict, None if confidence is None else float(confidence),
             _shape_of_edit(before, after),
             json.dumps({k: v for k, v in features.items() if v is not None})[:800]))
        return int(cur.lastrowid or 0)


def recent(limit: int = 100, kind: str = "") -> list[dict]:
    from . import store
    with store._connect() as conn:
        conn.executescript(_SCHEMA)
        sql = "SELECT * FROM experience"
        args: tuple = ()
        if kind:
            sql += " WHERE kind=?"
            args = (kind,)
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def rate(kind: str, target: str = "", least: int = 1) -> float | None:
    """Share of judged items he took AS IS. None when there is too little to say.

    "Too little to say" is the important half: three approvals is not a 100%
    approval rate, it is three approvals, and a learner acting on it would be
    acting on nothing. `ignored` is excluded — see the module docstring.
    """
    from . import store
    with store._connect() as conn:
        conn.executescript(_SCHEMA)
        marks = ",".join("?" * len(JUDGED))
        sql = f"SELECT verdict FROM experience WHERE kind=? AND verdict IN ({marks})"
        args: list = [kind, *JUDGED]
        if target:
            sql += " AND target=?"
            args.append(target)
        rows = [r["verdict"] for r in conn.execute(sql, args).fetchall()]
    if len(rows) < max(1, least):
        return None
    return rows.count("as_is") / len(rows)


def calibration() -> list[dict]:
    """Per confidence bucket: what Asta claimed, and what actually happened.

    [{claimed, n, actual}] — `actual` is the share taken as is. This is the
    number the phase exists for: without it, "0.95 confident" is a word.
    """
    from . import store
    with store._connect() as conn:
        conn.executescript(_SCHEMA)
        marks = ",".join("?" * len(JUDGED))
        rows = conn.execute(
            f"SELECT confidence, verdict FROM experience "
            f"WHERE confidence IS NOT NULL AND verdict IN ({marks})", JUDGED).fetchall()
    buckets: dict[float, list[str]] = {}
    for r in rows:
        edge = max([b for b in BUCKETS if float(r["confidence"]) >= b] or [0.0])
        buckets.setdefault(edge, []).append(r["verdict"])
    return [{"claimed": edge, "n": len(v), "actual": v.count("as_is") / len(v)}
            for edge, v in sorted(buckets.items()) if v]


def edits(kind: str = "send", limit: int = 200) -> dict[str, int]:
    """How he tends to change things: {"shorter": 12, "greeting removed": 9}.

    The fourth learner reads this — a habit he repeats is a rule Asta could
    have followed before he had to.
    """
    out: dict[str, int] = {}
    for row in recent(limit, kind):
        for note in (row["edit"] or "").split(", "):
            if note:
                out[note] = out.get(note, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
