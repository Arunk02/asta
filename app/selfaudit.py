"""Asta checks its own last day, so he is not the one finding each failure.

13 Oct: "im tired of fixing one by one issue in asta". Every failure fixed that
week was already in Asta's own records before he noticed it: asks dropped as
"nothing checkable", a finding sent to Deepa as a "question", "nothing was
sent" when something was, a task that timed out, a link it asked him for
instead of reading. This reads those records and says so — every evening, and
whenever he asks how it did — so the next one is found by Asta, not by him.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import time

from . import store

AUDIT_AT = os.environ.get("ASTA_SELF_AUDIT_AT", "18:30").strip()

#: Asta asking him for what it could have looked up: an id, a link, an environment.
_ASKED_HIM = re.compile(
    r"\b(?:can|could) you (?:share|send|paste|give)\b|\bwhich (?:workflow|booking|environment|env)\b"
    r"|\bI need the\b|\bshare the (?:workflow|link|id|booking)\b", re.I)


def _outcomes(since: float, kind: str, outcome: str) -> list[dict]:
    with store._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT subject, detail, created_at FROM outcomes WHERE kind=? AND outcome=? "
            "AND created_at > ? ORDER BY created_at", (kind, outcome, since))]


def findings(hours: float = 24.0) -> list[dict]:
    """What went wrong, by kind: [{kind, title, count, examples}]."""
    since = time.time() - hours * 3600
    out: list[dict] = []

    def add(kind: str, title: str, rows: list, fmt) -> None:
        if rows:
            out.append({"kind": kind, "title": title, "count": len(rows),
                        "examples": [fmt(r) for r in rows[-3:]]})

    skipped = [r for r in _outcomes(since, "responder", "skipped")
               if "nothing checkable" in (r["detail"] or "") or "incident asks" in (r["detail"] or "")]
    add("skipped_asks", "Colleague messages not looked into", skipped,
        lambda r: f"{r['subject']}: {r['detail'][:120]}")
    add("said_alone", "Lines sent without your yes", _outcomes(since, "thread", "said"),
        lambda r: f"{r['subject']}: {r['detail'][:120]}")
    held = [r for r in _outcomes(since, "thread", "held back") if "needs his yes" in (r["detail"] or "")]
    add("held", "Lines held for your yes", held, lambda r: f"{r['subject']}: {r['detail'][:120]}")
    false = (_outcomes(since, "turn", "false_send_claim") + _outcomes(since, "turn", "false_denial_of_send")
             + _outcomes(since, "turn", "false_schedule_claim"))
    add("false_claims", "Replies corrected for a false claim", false, lambda r: r["detail"][:120])
    with store._connect() as conn:
        failed = [dict(r) for r in conn.execute(
            "SELECT id, title, error FROM tasks WHERE status='failed' AND "
            "COALESCE(finished_at, created_at) > ? ORDER BY id", (since,))]
        replies = [dict(r) for r in conn.execute(
            "SELECT content, created_at FROM ui_messages WHERE role='assistant' AND created_at > ? "
            "AND (meta LIKE '%claude_cli%' OR meta LIKE '%copilot%' OR meta LIKE '%\"via\"%')",
            (since,))]
    add("failed_tasks", "Tasks that failed", failed,
        lambda r: f"#{r['id']} {r['title'][:70]} — {(r['error'] or '')[:60]}")
    asked = [r for r in replies if _ASKED_HIM.search(r["content"] or "") and "?" in (r["content"] or "")]
    add("asked_him", "Times it asked you for something it might have looked up", asked,
        lambda r: (r["content"] or "")[:140].replace("\n", " "))
    down = _outcomes(since, "teams", "activity_unavailable")
    if len(down) >= 5:
        add("teams_down", "Teams feed unreadable", down, lambda r: (r["detail"] or "")[:100])
    return out


def render(found: list[dict], hours: float = 24.0) -> str:
    if not found:
        return f"🩺 Last {hours:.0f}h: nothing went wrong that my records show."
    lines = [f"🩺 What went wrong in the last {hours:.0f}h, from my own records:"]
    for f in found:
        lines.append(f"- {f['title']}: {f['count']}")
        lines += [f"    · {e}" for e in f["examples"]]
    return "\n".join(lines)


async def daily_loop() -> None:
    """Once a day at AUDIT_AT: say what went wrong, only when something did."""
    from . import notify, wake
    while True:
        now = dt.datetime.now()
        if AUDIT_AT and now.strftime("%H:%M") == AUDIT_AT[:5] \
                and store.kv_get("self_audit_day") != now.date().isoformat():
            store.kv_set("self_audit_day", now.date().isoformat())
            with __import__("contextlib").suppress(Exception):
                found = findings(24)
                if found:
                    await notify.notify(render(found), "audit", urgency="ambient")
                store.record_outcome("audit", "daily", detail=f"{len(found)} kinds")
        await wake.sleep(50.0)
