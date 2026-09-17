"""What the three P7 graphs actually do — the graph owns the sequence, these
functions own the knowledge.

Deliberately thin. `followup` knows what a stalled PR looks like, `responder`
knows what is worth checking, `consent` and `policy` know what may leave the
house — and none of that moves in here. A graph that re-implemented any of it
would be the second copy of a decision, which is the bug that put a 20-minute
constant in two places and made Teams and chat disagree about the same message.

So each binding is: call the thing that already knows, and hand the graph back a
plain dict it can branch on.
"""

from __future__ import annotations

import time

from app import store

from . import draft_send, engine, follow_through, investigate


# --- follow through ---------------------------------------------------------

async def _watch(promise: dict) -> dict:
    """What the watcher sees now, as the graph's three facts."""
    from app import followup
    row = next((r for r in followup._load()
                if str(r.get("id")) == str(promise.get("id"))), None)
    if row is None:
        return {"state": "gone", "done": True}
    notes, done = await followup.check_one(row, time.time())
    followup._save([row if str(r.get("id")) == str(row.get("id")) else r
                    for r in followup._load()])
    return {"state": "; ".join(notes) or row.get("status", ""),
            "moved": bool(row.get("last_moved_at", 0) >= time.time() - 3600),
            "done": done}


async def _nudge(promise: dict):
    from app import followup
    row = next((r for r in followup._load()
                if str(r.get("id")) == str(promise.get("id"))), None)
    if row is None:
        return ""
    stuck = sum(1 for u in row.get("urls", [])
                if not (row.get("seen", {}).get(u) or "").startswith("MERGED"))
    return await followup._nudge(row, stuck, time.time())


async def _warn(promise: dict):
    """Before the deadline, while it is still something he can act on."""
    from app import notify
    left = max(0, int((float(promise.get("due_at") or 0) - time.time()) / 3600))
    await notify.notify(
        f"⏰ {promise.get('goal', 'your promise')} — {left}h to go, "
        f"{promise.get('state') or 'nothing has moved'}.\n"
        f"Say *chase them* or *leave it*.", "warn", urgency="direct")


async def tick_promise(row: dict) -> dict:
    """One look at one promise, through the graph, so a warning can WAIT.

    A tick is also where an answer he gave at a gate gets APPLIED: the front desk
    records it synchronously the moment he says it, and the thread picks it up
    here. Recorded first, applied second — a restart in between changes nothing.
    """
    follow_through.WATCHER = _watch
    follow_through.NUDGER = _nudge
    follow_through.WARNER = _warn
    fid = row["id"]
    if store.kv_get(engine.waiting_key("promise", fid)) and \
            engine.answer_waiting("promise", fid) is not None:
        return await engine.resume("promise", fid, follow_through.build)
    return await engine.drive("promise", fid, follow_through.build,
                              {"goal": row.get("goal", ""), "id": fid,
                               "person": row.get("person", ""),
                               "urls": row.get("urls") or [],
                               "due_at": row.get("due_at") or 0})


async def answer_promise(fid: int, text: str) -> dict:
    """What he said when it warned him — resumes the thread at its gate."""
    return await engine.answer("promise", fid, follow_through.build, {"text": text})


# --- investigation ----------------------------------------------------------

#: How long one look may run before the investigation takes what it has.
LOOK_TIMEOUT = 600

#: Statuses a look is finished in — read or gave up.
_LOOK_DONE = ("done", "failed", "rejected", "cancelled")


async def _looker(question: str, looks: list) -> dict:
    """One read-only look, run by the same engine a delegated question uses —
    and WAITED for.

    The first version spawned the analysis and returned at once with nothing
    found. `judge` saw an empty finding, looked again, and spawned again: one
    question would have become four analyses running side by side, each billing
    his subscription for the same answer. Caught before it was ever wired live.

    Two rules from the code graph apply. A look waits for its result. And a look
    that is RE-RUN — a restart mid-analysis re-enters the node from the top —
    reuses the analysis it already started rather than starting a second one,
    which is the #88/#89 failure in a new costume.
    """
    import asyncio
    import contextlib
    import hashlib

    from app import tasks

    idx = len(looks)
    mark = "inv_look:" + hashlib.sha1(question.encode()).hexdigest()[:16] + f":{idx}"
    already = "; ".join(str(lk.get("what", "")) for lk in looks)
    title = f"Check: {question[:60]}"
    tid = store.kv_get(mark)
    if not tid:
        prompt = (f"{question}\n\nRead-only. Say what you CHECKED and what you FOUND.\n"
                  + (f"Already checked, do not repeat: {already}\n" if already else ""))
        t = tasks.spawn(title, prompt, "analysis", None, "")
        tid = str(t["id"])
        store.kv_set(mark, tid)
    job = tasks._running.get(int(tid))
    if job is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(job), timeout=LOOK_TIMEOUT)
    row = store.get_task(int(tid)) or {}
    found = ((row.get("result") or "").strip()
             if row.get("status") in _LOOK_DONE else "")
    return {"what": title, "found": found[:1500], "task": int(tid), "at": time.time()}


async def investigate_question(key: str, question: str, who: str = "") -> dict:
    investigate.CHECKER = _looker
    return await engine.drive("investigate", key, investigate.build,
                              {"question": question, "who": who})


# --- draft and send ---------------------------------------------------------

def bind_send(write, send, confirm) -> None:
    """Point the graph at one channel's three acts, for this run."""
    draft_send.WRITER, draft_send.SENDER, draft_send.CONFIRMER = write, send, confirm


async def stage_draft(key: str, to: str, channel: str, body: str = "") -> dict:
    """Write it, put it in front of him, and stop there."""
    return await engine.drive("draft", key, draft_send.build,
                              {"to": to, "channel": channel, "body": body})


async def answer_draft(key: str, approved: bool, text: str = "") -> dict:
    """His word on a staged message. Recorded before it is applied."""
    return await engine.answer("draft", key, draft_send.build,
                               {"approved": approved, "text": text})


def waiting_on(kind: str, key) -> str:
    return store.kv_get(engine.waiting_key(kind, key)) or ""
