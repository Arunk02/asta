"""Never do the same investigation twice.

    think do we need cache or something if users asks same thing u shouldnt do
    the same operation multiple times

An investigation is the expensive thing Asta does: a whole brain run, minutes of
it, against his subscription's limit. Two people asking about the same booking
within the hour — or one person asking twice, in different words — used to be
two investigations.

What counts as "the same":

  * the same KIND of question (an incident check is not a PR review), and
  * the same THINGS it is about — the PR, ticket or booking id. "can you check
    booking 88271234?" and "pls look at 88271234 booking" are one question.
    Without an id, the distinctive words, order-free.

How long an answer stays true depends on what it is about, so each kind has its
own lifetime: production state goes stale in minutes, a PR review holds until
the PR changes (hours, approximated), a general answer for half an hour.

Two outcomes besides a miss:
  * RUNNING — the same investigation is already under way: join it. The new
    asker gets the answer when it lands; nothing new is spawned.
  * DONE and fresh — hand back what was found, with when it was found.

The answer itself is never copied here. It lives on the task row, which is the
single place a result is ever written, so the cache can never disagree with it.
"""

from __future__ import annotations

import hashlib
import time

from . import store

#: How long a finished answer stays true, by kind of question, in seconds.
TTL = {
    "incident": 15 * 60,       # production changes by the minute
    "debug": 15 * 60,
    "pr_review": 6 * 3600,     # holds until the PR changes
    "review_request": 30 * 86400,  # only used with a verified repo, head and CI fingerprint
    "ask": 30 * 60,
}
DEFAULT_TTL = 30 * 60

#: A task still "running" after this long has stalled; a new ask should not wait
#: behind it for ever.
RUNNING_FOR_AT_MOST = 60 * 60

_RUNNING = ("queued", "running", "planning", "working", "pending", "in_progress",
            "awaiting_input", "paused")
_DONE = ("done", "completed", "awaiting_approval")


def key_for(kind: str, text: str) -> str:
    """The same case, environment and question may share a finding."""
    from . import booking_case, knowledge, threads
    ents = threads.entities_in(text or "")
    if ents:
        subject = "|".join(ents)
        scope = booking_case.case_scope(text)
        if scope:
            subject += f"|env:{scope[1]}"
            generic = knowledge._CHATTER | {
                "look", "see", "tell", "know", "quick", "now", "booking", "bookings",
                "check", "why", "how", "can", "could", "please", "failed", "failure",
                "done", "status", "in", *booking_case._ENV_NAMES,
            }
            topics = {w for w in knowledge.terms(text[:350])
                      if w not in generic and w.upper() not in booking_case.ids(text)}
            subject += "|topic:" + "|".join(sorted(topics))
    else:
        chatter = knowledge._CHATTER | {"look", "see", "tell", "know", "quick", "now"}
        subject = "|".join(sorted({w for w in knowledge.terms(text or "") if w not in chatter}))
    digest = hashlib.sha1(f"{kind}|{subject}".encode()).hexdigest()[:16]
    return f"{kind}:{digest}"


def start(key: str, kind: str, task_id: int, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with store._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO result_cache (key, kind, task_id, created_at, "
                     "expires_at) VALUES (?,?,?,?,?)",
                     (key, kind, int(task_id), now, now + TTL.get(kind, DEFAULT_TTL)))


def lookup(key: str, now: float | None = None) -> dict | None:
    """{state: running|done, task_id, result?, at} — or None: go and find out."""
    now = time.time() if now is None else now
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM result_cache WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    row = dict(row)
    task = store.get_task(int(row["task_id"])) or {}
    status = (task.get("status") or "").lower()
    if status in _RUNNING and now - float(row["created_at"]) <= RUNNING_FOR_AT_MOST:
        return {"state": "running", "task_id": row["task_id"], "at": row["created_at"]}
    finished = float(task.get("finished_at") or row["created_at"])
    if status in _DONE and now <= float(row["expires_at"]) and \
            now - finished <= TTL.get(row["kind"], DEFAULT_TTL):
        return {"state": "done", "task_id": row["task_id"], "at": finished,
                "result": task.get("result") or ""}
    forget(key)                 # stale, failed or stalled: the next ask starts fresh
    return None


def forget(key: str) -> None:
    with store._connect() as conn:
        conn.execute("DELETE FROM result_cache WHERE key=?", (key,))
