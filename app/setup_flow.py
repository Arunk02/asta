"""Start, carry on and answer setup workflows (app/graph/setup.py).

A workflow waiting on someone else's approval has ENDED its run on purpose; the
ticker starts it again from the top, where every step first checks what is
already true, so a day-long wait and a restart in the middle of it cost nothing
and repeat nothing. His answers at a gate are written down before they are
applied (engine.answer), so one that arrives just before a restart still lands.
"""
from __future__ import annotations

import json

from app import store

KIND = "setup"
TICK_SECONDS = 600
_KEY = "setup_flow:"


def _save(wid: str, out: dict) -> dict:
    rec = {"outcome": out.get("outcome", ""), "waiting_at": out.get("waiting_at", ""),
           "detail": out.get("detail", ""), "granted": out.get("granted") or [],
           "pending": out.get("pending") or [], "goal": out.get("goal", "")}
    store.kv_set(_KEY + wid, json.dumps(rec))
    return rec


def status(wid: str) -> dict:
    try:
        return json.loads(store.kv_get(_KEY + wid) or "{}")
    except ValueError:
        return {}


async def start(wid: str, portal: str, doc: str, goal: str, authorized: dict) -> dict:
    """Begin. `authorized` must come from HIS instruction, never from the document."""
    from app.graph import engine, setup
    out = await engine.drive(KIND, wid, setup.build,
                             {"id": wid, "portal": portal, "doc": doc, "goal": goal,
                              "authorized": dict(authorized or {})})
    return _save(wid, out)


async def tick(wid: str) -> dict:
    """Look again at a workflow waiting on an approval."""
    from app.graph import engine, setup
    if status(wid).get("outcome") != "waiting":
        return status(wid)
    out = await engine.drive(KIND, wid, setup.build, {"outcome": "", "pending": []})
    return _save(wid, out)


async def answer(wid: str, value: dict) -> dict:
    """His answer at a gate: "yes"/"no" to a request, or "done" after signing in."""
    from app.graph import engine, setup
    return _save(wid, await engine.answer(KIND, wid, setup.build, value))


async def resume(wid: str) -> dict:
    """After a restart: apply an answer written down before it, or carry on."""
    from app.graph import engine, setup
    return _save(wid, await engine.resume(KIND, wid, setup.build))


def report(wid: str, rec: dict) -> str:
    """The line he reads: the verified result, or the concrete thing still missing."""
    goal = rec.get("goal") or wid
    if rec.get("outcome") == "done":
        return f"✅ Set up and verified — {goal}. {rec.get('detail', '')}".strip()
    if rec.get("outcome") == "waiting":
        return f"⏳ {goal}: waiting on approval for {', '.join(rec.get('pending') or [])}."
    if rec.get("waiting_at") == "login":
        return f"🔑 {goal}: your sign-in to the portal expired. Sign in, then say done."
    if (rec.get("waiting_at") or "").startswith("request:"):
        return f"❓ {goal}: request access to {rec['waiting_at'].split(':', 1)[1]}?"
    return f"⛔ {goal}: not set up — {rec.get('detail', 'stopped')}."


async def tick_all() -> list[str]:
    """Every waiting workflow, once. Returns what changed, as lines he reads."""
    lines = []
    from app.graph import engine
    for key in store.kv_like(_KEY + "%"):
        wid = key[len(_KEY):]
        before = status(wid)
        if before.get("waiting_at") and engine.answer_waiting(KIND, wid):
            lines.append(report(wid, await resume(wid)))       # his answer, applied
            continue
        if before.get("outcome") != "waiting":
            continue
        after = await tick(wid)
        if after.get("outcome") != "waiting" or after.get("waiting_at"):
            lines.append(report(wid, after))
    return lines


async def loop() -> None:
    from app import notify, wake
    while True:
        await wake.sleep(TICK_SECONDS)
        try:
            for line in await tick_all():
                await notify.notify(line, "task", urgency="direct")
        except Exception:                                    # noqa: BLE001
            continue
