"""The digest: everything worth knowing that is not worth interrupting him for.

P5 of the Astra-class plan. Asta pushed about 70 things a day and he ignored 38%
of what it tracked — so the filter was not "is this real", it was "is this worth
a buzz right now". Three things now land here instead of on his phone:

  * a source he has ignored again and again (app/attention.py decides, from his
    own reactions — not a rule somebody wrote);
  * anything past the day's push budget, once the important ones have been spent;
  * anything an explicit rule of his sends here.

A digest goes out at midday and in the evening, and whatever is left rides in the
morning brief. One buzz carrying eight lines beats eight buzzes, and nothing is
dropped: the UI bell and the ledger already have every item.
"""

from __future__ import annotations

import json
import time

from . import store

KEY = "digest_pending"
MAX_ITEMS = 60
#: When the digest goes out, as local hours. The morning brief carries the rest.
MIDDAY, EVENING = 13, 19


def add(text: str, source: str = "", why: str = "") -> None:
    """Hold one item for the next digest. The bell and the ledger already have it."""
    line = " ".join((text or "").split())
    if not line:
        return
    rows = pending()
    rows.append({"at": time.time(), "text": line[:400], "source": source[:60], "why": why[:80]})
    store.kv_set(KEY, json.dumps(rows[-MAX_ITEMS:]))


def pending() -> list[dict]:
    try:
        rows = json.loads(store.kv_get(KEY) or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("text")]


def take() -> list[dict]:
    rows = pending()
    store.kv_set(KEY, "[]")
    return rows


def render(rows: list[dict], reason: str = "") -> str:
    """One message he can read on a phone: grouped by where it came from."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r.get("source") or "other", []).append(r)
    head = f"📥 Digest — {len(rows)} thing{'s' if len(rows) != 1 else ''} you did not need to be interrupted for"
    if reason:
        head += f" ({reason})"
    out = [head]
    for source, items in by.items():
        out.append(f"\n*{source}* ({len(items)})")
        for r in items[:8]:
            out.append(f"• {r['text'][:120]}")
        if len(items) > 8:
            out.append(f"  …and {len(items) - 8} more")
    return "\n".join(out)


async def flush(reason: str = "") -> dict:
    """Send the digest now, or report that there was nothing to send."""
    rows = take()
    if not rows:
        return {"sent": False, "items": 0}
    from . import notify
    body = render(rows, reason)
    # The bell gets it too. Delivering below `notify` is deliberate — re-entering
    # it would weigh a digest against the budget it exists to absorb — but that
    # also skipped the one line every other push takes: found live on 16 Sep,
    # when a digest of three real messages reached his phone and appeared
    # nowhere in the UI.
    store.add_notification(body, "digest")
    out = await notify.deliver(body)
    store.record_outcome("digest", "sent", detail=f"{len(rows)} items {reason}"[:200])
    return {"sent": True, "items": len(rows), **out}


def _slot(now: float | None = None) -> str:
    hour = time.localtime(now if now is not None else time.time()).tm_hour
    if hour >= EVENING:
        return "evening"
    if hour >= MIDDAY:
        return "midday"
    return ""


def due(now: float | None = None) -> str:
    """Which digest slot is due and has not gone out today — '' when none is."""
    slot = _slot(now)
    if not slot:
        return ""
    day = time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
    return "" if store.kv_get(f"digest_sent:{day}:{slot}") else slot


def mark_sent(slot: str, now: float | None = None) -> None:
    day = time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
    store.kv_set(f"digest_sent:{day}:{slot}", "1")


async def tick(now: float | None = None) -> dict:
    """Send the midday or evening digest when its hour has come. Called by the loop."""
    slot = due(now)
    if not slot or not pending():
        return {"sent": False, "items": 0}
    mark_sent(slot, now)
    return await flush(reason=f"{slot} digest")


async def loop(interval: int = 300) -> None:
    import asyncio
    while True:
        await asyncio.sleep(interval)
        try:
            await tick()
        except Exception:                                   # noqa: BLE001
            pass
