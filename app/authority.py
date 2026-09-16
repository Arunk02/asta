"""What Asta may do without asking — explicit, capped, revocable, and earned.

The other half of P5. Everything outward is staged for his yes, which is right
for anything that matters and silly for the tenth identical nudge: "may nudge a
colleague about a PR review once a day" is a decision he can make once.

Three properties keep that from becoming a licence:

  EARNED. A permission is only ever PROPOSED after ten of that exact kind — same
  act, same person — have been approved as-is. Never for a new recipient, never
  for a new kind of act, never proposed twice.

  CAPPED. Each grant carries a daily limit. Past it, the act is staged for his
  yes again, exactly as before.

  VISIBLE AND REVERSIBLE. Every use is recorded and reported in the morning line;
  "my permissions" lists them and "revoke 2" ends one. Nothing here can be
  created by Asta alone: a grant exists only after he says yes (ops.rule_add's
  sibling, `authority_grant`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import store

#: Approved-as-is repeats of the exact same act before Asta may even ask.
EARNED_AFTER = 10


@dataclass(frozen=True)
class Grant:
    id: int
    act: str
    target: str
    per_day: int
    words: str = ""

    def render(self) -> str:
        verb = {"send": "message", "call": "call", "comment": "comment on"}.get(self.act, self.act)
        return f"May {verb} {self.target} without asking — up to {self.per_day} a day"


def _row(r) -> Grant:
    return Grant(id=r["id"], act=r["act"], target=r["target"],
                 per_day=int(r["per_day"] or 1), words=r["words"] or "")


def grants() -> list[Grant]:
    with store._connect() as conn:
        rows = conn.execute("SELECT * FROM authority WHERE active=1 ORDER BY id").fetchall()
    return [_row(r) for r in rows]


def grant(act: str, target: str, per_day: int = 1, words: str = "") -> Grant:
    """Record a permission HE approved. Never called on Asta's own initiative."""
    act, target = (act or "").strip().lower(), (target or "").strip()
    if not act or not target:
        raise ValueError("a permission needs an act and who it is for")
    for g in grants():
        if g.act == act and g.target.lower() == target.lower():
            return g
    with store._connect() as conn:
        cur = conn.execute("INSERT INTO authority (act, target, per_day, words, created_at) "
                           "VALUES (?,?,?,?,?)",
                           (act, target, max(1, int(per_day)), words[:300], time.time()))
        gid = cur.lastrowid
    store.record_outcome("authority", "granted", subject=str(gid), detail=f"{act} {target}"[:200])
    return next(g for g in grants() if g.id == gid)


def revoke(grant_id: int) -> bool:
    with store._connect() as conn:
        row = conn.execute("SELECT id FROM authority WHERE id=? AND active=1",
                           (grant_id,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE authority SET active=0 WHERE id=?", (grant_id,))
    store.record_outcome("authority", "revoked", subject=str(grant_id))
    return True


def _used_key(gid: int, now: float | None = None) -> str:
    return f"authority_used:{gid}:" + time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def used_today(gid: int, now: float | None = None) -> int:
    try:
        return int(store.kv_get(_used_key(gid, now)) or 0)
    except ValueError:
        return 0


def may(act: str, target: str, now: float | None = None) -> Grant | None:
    """The grant that covers this act right now, or None — ask him as usual.

    His standing rules outrank a permission: a rule that says never message
    someone is not overridden by a convenience he granted for someone else.
    """
    from . import policy
    act, target = (act or "").lower(), (target or "")
    if not policy.check(act, target).ok:
        return None
    for g in grants():
        if g.act == act and g.target.lower() == target.lower().strip() \
                and used_today(g.id, now) < g.per_day:
            return g
    return None


def note_use(g: Grant, what: str = "", now: float | None = None) -> None:
    store.kv_set(_used_key(g.id, now), str(used_today(g.id, now) + 1))
    store.record_outcome("authority", "used", subject=str(g.id), detail=what[:200])


def note_approved(act: str, target: str) -> int:
    """He approved one of these as-is. Returns how many in a row that makes."""
    act, target = (act or "").lower(), (target or "").strip()
    if not act or not target:
        return 0
    key = f"authority_seen:{act}:{target.lower()}"
    try:
        n = int(store.kv_get(key) or 0) + 1
    except ValueError:
        n = 1
    store.kv_set(key, str(n))
    return n


def earned(act: str, target: str) -> bool:
    """Enough identical approvals to be worth asking about — and not asked before."""
    key = f"authority_seen:{(act or '').lower()}:{(target or '').strip().lower()}"
    try:
        n = int(store.kv_get(key) or 0)
    except ValueError:
        n = 0
    if n < EARNED_AFTER or may(act, target):
        return False
    return not store.kv_get(f"authority_proposed:{(act or '').lower()}:{(target or '').strip().lower()}")


def propose(act: str, target: str, per_day: int = 1) -> str:
    """Ask, once, for a permission he has approved ten of by hand."""
    from . import offers
    store.kv_set(f"authority_proposed:{act.lower()}:{target.strip().lower()}", "1")
    g = Grant(0, act, target, per_day)
    offers.staged_write("authority_grant",
                        {"act": act, "target": target, "per_day": per_day,
                         "words": f"after {EARNED_AFTER} you approved as-is"},
                        subject="a standing permission",
                        context=f"You have approved {EARNED_AFTER} of these as-is.",
                        question=f"{g.render()}?")
    store.record_outcome("authority", "proposed", detail=f"{act} {target}"[:200])
    return f"🔓 {g.render()}? Reply yes to allow it, or no."


async def send_now(g: Grant, to: str, what: str, to_group: bool = False) -> str:
    """Send under a permission, count it, and tell him it went — after it has."""
    from . import notify, ops
    line = await ops.REGISTRY["teams_send"]["run"](to=to, text=what, to_group=to_group)
    note_use(g, f"{to}: {what[:80]}")
    await notify.notify(f"📨 Sent to {to} under permission {g.id} "
                        f"({g.render()}):\n\n{what[:300]}\n\nSay “revoke {g.id}” to end it.",
                        "teams", urgency="direct", considered=True)
    return line


def summary() -> str:
    live = grants()
    if not live:
        return "Asta has no standing permissions — everything outward waits for your yes."
    lines = [f"  {g.id}. {g.render()} (used {used_today(g.id)} today)" for g in live]
    return "Asta may do these without asking:\n" + "\n".join(lines) + "\nSay “revoke <n>” to end one."
