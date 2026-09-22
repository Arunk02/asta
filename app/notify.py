"""Notifications: stored for the UI bell, and fanned out to WhatsApp + Telegram."""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx

from . import store, telegram, wa_format


def bridge_url() -> str:
    return os.environ.get("WA_BRIDGE_URL", "http://127.0.0.1:8323")


async def wa_send(text: str) -> bool:
    """Push a message through the WhatsApp bridge; False if bridge is down/unpaired.

    Markup is converted at this boundary, not by callers. WhatsApp is the only
    channel that needs it and every caller writes markdown, so doing it here is
    the difference between one conversion and thirty missed ones — the plan
    pushes had been arriving with `**bold**` spelled out in literal asterisks.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{bridge_url()}/send",
                json={"text": wa_format.for_whatsapp(text)},
                headers={"Authorization": "Bearer " + os.environ.get("ASTA_TOKEN", "")},
            )
            return r.status_code == 200
    except Exception:
        return False


async def wa_document(path: str, caption: str = "") -> bool:
    """Send a FILE to his phone. False when the bridge is down or unpaired."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{bridge_url()}/send-document",
                             headers={"Authorization": f"Bearer {os.environ.get('ASTA_TOKEN', '')}"},
                             json={"path": str(path), "caption": caption})
        return bool(r.status_code == 200 and (r.json() or {}).get("ok"))
    except Exception:                                           # noqa: BLE001
        return False


async def wa_voice(path: str, seconds: int = 1) -> bool:
    """Send a voice note (PTT) to his phone. False when the bridge will not take it."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{bridge_url()}/send-voice",
                             headers={"Authorization": f"Bearer {os.environ.get('ASTA_TOKEN', '')}"},
                             json={"path": str(path), "seconds": int(seconds)})
        return bool(r.status_code == 200 and (r.json() or {}).get("ok"))
    except Exception:                                           # noqa: BLE001
        return False


async def wa_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{bridge_url()}/status")
            return r.json()
    except Exception:
        return {"up": False, "paired": False}


async def wa_create_group(name: str) -> dict | None:
    """Ask the bridge to create the dedicated assistant group chat; None if bridge down."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{bridge_url()}/create-group",
                json={"name": name},
                headers={"Authorization": "Bearer " + os.environ.get("ASTA_TOKEN", "")},
            )
            return r.json()
    except Exception:
        return None


async def wa_config(changes: dict) -> dict | None:
    """Push config (enabled / allowed_jid) to the bridge; None if bridge is down."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(
                f"{bridge_url()}/config",
                json=changes,
                headers={"Authorization": "Bearer " + os.environ.get("ASTA_TOKEN", "")},
            )
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


HELD_KEY = "held_ambient_notifications"
HELD_MAX = 40


def hold_max_minutes() -> int:
    """How long an ambient item may sit held before it is delivered anyway.

    Presence was the ONLY release condition, and `at_laptop()` is true whenever he
    is touching the machine — including the afternoons he shuts Teams and Outlook
    and works on something else entirely. Held items could therefore wait hours
    for a departure that never came. A hold is a courtesy, not a black hole: past
    this age it goes out regardless of where he is. 0 disables the age release.
    """
    try:
        return max(0, int(os.environ.get("ASTA_HOLD_MAX_MINUTES", "45")))
    except ValueError:
        return 45


def _held_items() -> list[dict]:
    """Held entries as {at, text}. Tolerates the old bare-string format."""
    try:
        raw = json.loads(store.kv_get(HELD_KEY) or "[]")
    except Exception:
        return []
    out = []
    for it in raw:
        if isinstance(it, dict) and it.get("text"):
            out.append({"at": float(it.get("at") or 0), "text": it["text"]})
        elif isinstance(it, str) and it:
            out.append({"at": 0.0, "text": it})   # legacy: age unknown → overdue
    return out


def _stale(items: list[dict], now: float | None = None) -> bool:
    """True when the oldest held item has waited longer than the courtesy window."""
    limit = hold_max_minutes()
    if limit <= 0 or not items:
        return False
    now = time.time() if now is None else now
    return any(now - it["at"] >= limit * 60 for it in items)


def _hold(text: str) -> None:
    held = _held_items()
    held.append({"at": time.time(), "text": text})
    store.kv_set(HELD_KEY, json.dumps(held[-HELD_MAX:]))


async def deliver(text: str, *, force: bool = False, keys: tuple = ()) -> dict:
    """Actually put it on his phone, and remember that something just went out.

    Split out of `notify` so the coalescing flush can send a merged batch through
    exactly the same path — and so `note_sent` is stamped in ONE place. Stamping
    it per call site is how a batching window starts disagreeing with itself.

    The ONE door: the digest, a batch, a held flush and `notify` itself all end
    here, so a quiet rule checked here holds for every one of them. On 19 Sep
    the digest and the batch flush called this directly and went around every
    check `notify` made. `force` is only for the summary a quiet rule releases.
    """
    from . import budget, delivery
    if not force and quiet_rule() is not None:
        _keep_for_quiet(text, "batch", keys)
        return {"bell": True, "held": True, "quiet": True, "whatsapp": False, "telegram": False}
    wa = await wa_send(text)
    tg = await telegram.send(text)
    delivery.note_sent()
    budget.note_push()          # one buzz, one unit — a batch of four costs one
    if not (wa or tg):
        store.kv_set("last_push_failure",
                     json.dumps({"at": time.time(), "text": text[:120]}))
    return {"bell": True, "held": False, "whatsapp": wa, "telegram": tg}


def _ledger_priority(urgency: str, priority: int | None) -> int:
    """Translate a push's urgency into the ledger's ranking.

    `notify` has always spoken in urgency ("is this addressed to him?"); the
    ledger ranks by what it costs to miss. They are close enough to map, and
    mapping here keeps every caller speaking the vocabulary it already uses.
    """
    from . import attention
    if priority is not None:
        return int(priority)
    return attention.P_TODAY if urgency == "direct" else attention.P_FYI


async def notify(text: str, level: str = "info", urgency: str = "direct",
                 priority: int | None = None, *, source: str = "",
                 key: str = "", considered: bool = False, asked: bool = False,
                 keys: tuple | list = ()) -> dict:
    """Record for the UI bell and fan out to WhatsApp + Telegram.

    urgency="direct"  — someone is actually addressing Arun (1:1 message, @mention,
        mail to him), or he asked for it. Always delivered immediately.
    urgency="ambient" — useful-but-not-addressed-to-him (CI results, general channel
        traffic). Delivered only when he's AWAY from the laptop; while he's sitting
        there it is held, because he'd rather ask than be pinged. Held items are
        released the moment he steps away, and are always in the UI bell meanwhile.

    Returns which channels actually took it. This used to return None, so a fire
    that reached NOBODY (both channels down) looked identical to one delivered —
    which is exactly how reminders "sent" for days while landing only in a bell
    no one was looking at.
    """
    store.add_notification(text, level)  # the bell always gets everything
    keys = tuple(keys or ()) + ((key,) if key else ())

    # The ledger decides WHETHER, the same way `delivery` below decides WHEN.
    # It used to be consulted by three call sites out of fifty-six, so the
    # cross-source deduplication it exists for — one incident arriving as mail
    # AND as a Teams mention — covered two sources and nothing else. Asking here
    # means every source is covered by construction rather than by each caller
    # remembering to ask, which is how `delivery` was already done correctly.
    #
    # `considered=True` is the opt-out for the callers that already asked; a
    # second upsert of the same key would read as "already notified" and
    # suppress the very push their own check had just approved.
    #
    # An unnamed source is Asta speaking on its own initiative, and is filed as
    # such. Every one of the fifty-odd call sites here is Asta ANNOUNCING —
    # a health report, a finished task, a meeting reminder, a question it is
    # asking — and none of it is owed back to him. Calling it "notify" put
    # Asta's own voice in his backlog and made the chase loop chase its own
    # last chase; see attention.SELF_SOURCE for what that produced.
    if not considered:
        from . import attention, triage
        ledger_key = key or triage.stable_key(text)
        keys = keys + (ledger_key,)
        from . import clip as clip_mod
        if not attention.consider(source or attention.SELF_SOURCE, ledger_key,
                                  # `what` is rendered back to him in chases and
                                  # in "what's on my plate", so it must read as a
                                  # sentence rather than stop mid-word.
                                  what=clip_mod.clip(text, 200),
                                  priority=_ledger_priority(urgency, priority)):
            # Recorded, and deliberately not pushed. The bell above still has it,
            # so nothing is lost — it simply does not buzz twice for one thing.
            return {"bell": True, "held": False, "suppressed": True,
                    "whatsapp": False, "telegram": False}

    # His quiet time — a rule he made ("weekends off, summarise on Monday"). It
    # outranks everything below it, breakage included: he is off. What arrives
    # is kept and lands in one summary when the rule ends. `asked` is his own
    # ask coming back to him — a reminder he set still rings.
    if quiet_rule() is not None:
        if not asked and not (level in HIS_WORK and talking_to_asta()):
            _keep_for_quiet(text, level, keys)
            return {"bell": True, "held": True, "quiet": True, "whatsapp": False, "telegram": False}
        # Straight out, past the batch: nothing else is going to his phone today
        # for it to ride along with, and the door itself would hold it.
        return await deliver(text, force=True, keys=keys)

    from . import budget, delivery
    # The day's budget of interruptions. Breakage and things he is blocked on are
    # never counted against it; everything ordinary that arrives once the budget
    # is gone is read in the digest instead of buzzing his pocket.
    if not budget.allows(priority, urgency, level=level):
        from . import digest
        digest.add(text, source=level, why="past today's budget of interruptions", keys=keys)
        return {"bell": True, "held": True, "digested": True,
                "whatsapp": False, "telegram": False}
    # Night first, because it outranks every other reason to speak. Held items
    # wait for morning rather than for him to walk away — at 2am he has already
    # walked away, and a departure-released hold would fire instantly.
    if delivery.hold_for_quiet(urgency, priority):
        _hold(text)
        return {"bell": True, "held": True, "whatsapp": False, "telegram": False}
    # A direct Teams message while he is at the laptop: he may be answering it
    # in Teams right now, and a WhatsApp push a minute later is the same news
    # twice. It waits a moment, and goes only if he has not answered.
    if level == "teams" and urgency == "direct" and keys and REPLY_GRACE > 0:
        from . import presence
        if await presence.at_laptop():
            _hold_for_reply(text, keys)
            return {"bell": True, "held": True, "grace": True, "whatsapp": False, "telegram": False}
    if urgency == "ambient":
        from . import presence
        if await presence.at_laptop():
            held = _held_items()
            held.append({"at": time.time(), "text": text})
            held = held[-HELD_MAX:]
            # Holding is a courtesy with an expiry. If something has now waited out
            # the window, release the whole batch rather than keeping it hostage to
            # a departure that may not come today.
            if _stale(held):
                store.kv_set(HELD_KEY, json.dumps(held))
                # Report what the flush actually achieved. This used to claim
                # both channels had taken it without asking either — the one
                # shape of lie this module was written to end.
                return {"bell": True, **await flush_held(reason="waited long enough")}
            store.kv_set(HELD_KEY, json.dumps(held))
            return {"bell": True, "held": True, "whatsapp": False, "telegram": False}
    # Something went out moments ago and this is not breakage: let it ride along
    # with the next flush. One buzz carrying four items beats four buzzes.
    if delivery.should_batch(priority):
        delivery.buffer(text)
        return {"bell": True, "held": True, "batched": True,
                "whatsapp": False, "telegram": False}
    return await deliver(text)


async def live_push_channels() -> list[str]:
    """Which phone channels are actually connected right now (a real probe, not
    just configured). Used to tell the truth at the moment a reminder is set,
    rather than promising delivery that cannot happen."""
    out: list[str] = []
    try:
        st = await wa_status()
        if st.get("up") and st.get("paired"):
            out.append("WhatsApp")
    except Exception:
        pass
    if telegram.enabled() and telegram.chat_id():
        out.append("Telegram")
    return out


async def flush_held(reason: str = "while you were at the laptop") -> dict:
    """Deliver notifications that were held. Says WHY they are arriving now.

    Returns what actually happened, and puts the batch BACK when nothing reached
    him. The queue used to be cleared before the send and both results thrown
    away, so a flush with WhatsApp unpaired and Telegram unbound deleted the
    whole batch and reported success to a caller that then told him it was
    delivered. Held items are the ones deliberately kept back — losing those is
    losing the only copy of something Asta chose not to say at the time.

    The batch is capped, so putting it back cannot grow without bound.
    """
    from . import delivery
    held = _held_items()
    # Never in the small hours. This is the other half of the quiet-hours hold:
    # releasing on departure would fire the moment he goes to bed, which is the
    # exact opposite of what holding it was for.
    if not held or delivery.quiet_now():
        return {"held": bool(held), "whatsapp": False, "telegram": False}
    store.kv_set(HELD_KEY, "[]")
    texts = [it["text"] for it in held]
    head = f"🔕 Held ({len(texts)}) — {reason}:\n\n"
    body = "\n\n".join(texts[-10:])
    if len(texts) > 10:
        body += f"\n\n(+{len(texts) - 10} more in the app)"
    # Through the one door: quiet rules hold it, the budget counts it — this
    # flush used to send straight to WhatsApp, past both.
    out = await deliver(head + body)
    if out.get("quiet"):
        return {"held": True, "whatsapp": False, "telegram": False}
    if not (out["whatsapp"] or out["telegram"]):
        store.kv_set(HELD_KEY, json.dumps(held[-HELD_MAX:]))   # nothing landed — keep it
        return {"held": True, "whatsapp": False, "telegram": False}
    return {"held": False, "whatsapp": out["whatsapp"], "telegram": out["telegram"]}


async def held_watch_loop() -> None:
    """Release held ambient notifications — on departure OR on age.

    Two release conditions, because presence alone strands things: he can sit at
    the laptop all afternoon with Teams and Outlook closed, and a departure-only
    rule would keep every held item until evening.
    """
    from . import presence
    was_present = True
    while True:
        await asyncio.sleep(60)
        try:
            present = await presence.at_laptop()
            if was_present and not present:
                await flush_held()
            elif _stale(_held_items()):
                await flush_held(reason="waited long enough")
            was_present = present
            await release_grace()
        except Exception:
            pass


# --- quiet time, and not telling him twice -------------------------------------------

QUIET_KEY = "quiet_held"
QUIET_MAX = 200
GRACE_KEY = "grace_pending"

#: Seconds a direct Teams message waits, while he is at the laptop, for him to
#: answer it in Teams before it goes to his phone. 0 = send at once, as before.
REPLY_GRACE = int(os.environ.get("ASTA_REPLY_GRACE", "180"))


#: What Asta is doing FOR him — a task he started, a call he asked for. On a
#: quiet Saturday he can still ask Asta for something, and the answer must not
#: wait for Monday; colleagues, CI and meetings still do.
HIS_WORK = ("task", "action", "calls", "reply", "files")
ENGAGED_SECONDS = 1800


def talking_to_asta(now: float | None = None) -> bool:
    """He has written to Asta in the last half hour."""
    now = time.time() if now is None else now
    return now - store.last_user_message_at() < ENGAGED_SECONDS


def quiet_rule(now: float | None = None):
    """The quiet rule holding right now (or at `now`), or None."""
    from . import policy
    return policy.quiet_holding(time.time() if now is None else now)


def _items(key: str) -> list[dict]:
    try:
        rows = json.loads(store.kv_get(key) or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("text")]


def _keep_for_quiet(text: str, level: str, keys=()) -> None:
    rows = _items(QUIET_KEY)
    rows.append({"at": time.time(), "text": text, "level": level, "keys": list(keys or ())})
    store.kv_set(QUIET_KEY, json.dumps(rows[-QUIET_MAX:]))


def answered(keys) -> bool:
    """He has dealt with every ledger item behind this push. A push with no
    keys is never "answered".

    "acted", not "settled": the hourly sweep drops anything unanswered for a
    week as ignored, and a week of quiet is exactly when he was not there to
    answer — dropped would have thinned the very summary he asked for.
    """
    from . import attention
    keys = [k for k in (keys or ()) if k]
    if not keys:
        return False
    for k in keys:
        row = store.attention_get(k)
        if not row:
            return False
        if row.get("state") == "acted":
            continue
        if not attention.he_replied_since(row):
            return False
        attention.mark_acted(k, why="he replied")
    return True


def _hold_for_reply(text: str, keys) -> None:
    rows = _items(GRACE_KEY)
    rows.append({"at": time.time(), "text": text, "keys": list(keys or ())})
    store.kv_set(GRACE_KEY, json.dumps(rows[-HELD_MAX:]))


async def release_grace(now: float | None = None) -> int:
    """Send the Teams messages he has not answered after the grace; drop the ones
    he has. Returns how many went out."""
    now = time.time() if now is None else now
    sent, keep = 0, []
    for it in _items(GRACE_KEY):
        if answered(it.get("keys")):
            store.record_outcome("attention", "answered in Teams — not pushed",
                                 detail=it["text"][:120])
            continue
        if now - float(it.get("at") or 0) >= REPLY_GRACE:
            await deliver(it["text"], keys=tuple(it.get("keys") or ()))
            sent += 1
            continue
        keep.append(it)
    store.kv_set(GRACE_KEY, json.dumps(keep))
    return sent


async def release_quiet(now: float | None = None) -> dict:
    """When his quiet time has ended: everything it held, in one message.

    Everything still unanswered goes in — the pushes the rule stopped and the
    digest that could not go out — first what came from people, then the rest.
    """
    from . import digest
    from . import policy
    now = time.time() if now is None else now
    if quiet_rule(now) is not None:
        return {"sent": False, "items": 0}
    policy.expire_quiet(now)
    held = [r for r in _items(QUIET_KEY) if not answered(r.get("keys"))]
    pending = digest.take() if held else []
    if not held and not pending:
        store.kv_set(QUIET_KEY, "[]")
        return {"sent": False, "items": 0}
    store.kv_set(QUIET_KEY, "[]")
    first = min((r["at"] for r in held), default=now)
    since = time.strftime("%a %d %b %H:%M", time.localtime(first))
    people = [r for r in held if r.get("level") in ("teams", "teams-chat", "outlook", "call")]
    rest = [r for r in held if r not in people]
    lines = [f"🗓 While you were off (since {since}): {len(held) + len(pending)} things."]
    if people:
        lines.append("\n*From people*")
        lines += [f"• {r['text'][:200]}" for r in people[:15]]
    if rest:
        lines.append("\n*Everything else*")
        lines += [f"• {r['text'][:160]}" for r in rest[:15]]
    if pending:
        lines.append("\n" + digest.render(pending, "held while you were off"))
    body = "\n".join(lines)
    store.add_notification(body, "digest")
    out = await deliver(body, force=True)
    # He is hearing about these now, not when they arrived: the week-old sweep
    # would otherwise label what a long quiet held back as "ignored" the hour
    # it reached him, and teach the filter that its senders are noise.
    for r in held:
        for k in r.get("keys") or ():
            store.attention_set(k, notified_at=now)
    store.record_outcome("quiet", "released", detail=f"{len(held)} held, {len(pending)} digest")
    return {"sent": True, "items": len(held) + len(pending), **out}
