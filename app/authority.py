"""What Asta may do without asking — explicit, capped, revocable, and earned.

The other half of P5. Outward acts need his yes or an explicit, narrow standing
exception. A grant has a daily cap; his private automatic Teams reply list is
limited to exact 1:1 recipients, with no grant for groups or other operations.

Three properties keep that from becoming a licence:

  EARNED. A permission is only ever PROPOSED after ten of that exact kind — same
  act, same person — have been approved as-is. Never for a new recipient, never
  for a new kind of act, never proposed twice. This only governs unsolicited
  proposals: a standing permission HE explicitly requests takes effect immediately.

  CAPPED. Each grant carries a daily limit. Past it, the act is staged for his
  yes again, exactly as before.

  VISIBLE AND REVERSIBLE. Every use is recorded and reported in the morning line;
  "my permissions" lists them and "revoke 2" ends one. Nothing here can be
  created by Asta alone: a grant exists only after he says yes (ops.rule_add's
  sibling, `authority_grant`).
"""

from __future__ import annotations

import re
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
    expires_at: float | None = None

    def render(self) -> str:
        verb = {"send": "message", "call": "call", "comment": "comment on"}.get(self.act, self.act)
        until = (f", until {time.strftime('%H:%M', time.localtime(self.expires_at))} today"
                 if self.expires_at else "")
        return f"May {verb} {self.target} without asking — up to {self.per_day} a day{until}"


def _row(r) -> Grant:
    keys = r.keys() if hasattr(r, "keys") else []
    return Grant(id=r["id"], act=r["act"], target=r["target"],
                 per_day=int(r["per_day"] or 1), words=r["words"] or "",
                 expires_at=r["expires_at"] if "expires_at" in keys else None)


def _live(r, now: float | None = None) -> bool:
    """Active, and not past an end he gave it ("just for today")."""
    end = r["expires_at"] if "expires_at" in r.keys() else None
    return bool(r["active"]) and (end is None or (time.time() if now is None else now) < float(end))


def grants() -> list[Grant]:
    with store._connect() as conn:
        rows = conn.execute("SELECT * FROM authority WHERE active=1 ORDER BY id").fetchall()
    return [_row(r) for r in rows if _live(r)]


def grant(act: str, target: str, per_day: int = 1, words: str = "",
          expires_at: float | None = None) -> Grant:
    """Record a permission HE approved. Never called on Asta's own initiative."""
    act, target = (act or "").strip().lower(), (target or "").strip()
    if not act or not target:
        raise ValueError("a permission needs an act and who it is for")
    for g in grants():
        if g.act == act and g.target.lower() == target.lower():
            if expires_at and g.expires_at and expires_at > g.expires_at:
                with store._connect() as conn:          # "for today" again, later: extend it
                    conn.execute("UPDATE authority SET expires_at=? WHERE id=?", (expires_at, g.id))
                return next(x for x in grants() if x.id == g.id)
            return g
    with store._connect() as conn:
        cur = conn.execute("INSERT INTO authority (act, target, per_day, words, created_at, "
                           "expires_at) VALUES (?,?,?,?,?,?)",
                           (act, target, max(1, int(per_day)), words[:300], time.time(),
                            expires_at))
        gid = cur.lastrowid
    store.record_outcome("authority", "granted", subject=str(gid), detail=f"{act} {target}"[:200])
    return next(g for g in grants() if g.id == gid)


def explicitly_names(words: str, target: str) -> bool:
    """A full recipient name, never a substring or an ambiguous first name."""
    name = " ".join((target or "").split())
    return bool(name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)",
                                  " ".join((words or "").split()), re.I))


def requested_followup(words: str, target: str) -> bool:
    from . import consent
    verbs = re.compile(r"\b(?:follow[ -]?up(?:\s+with)?|chase|keep\s+(?:checking|following))\b", re.I)
    return any(explicitly_names(clause, target) and any(
        not consent._negated(clause, m.start()) for m in verbs.finditer(clause))
        for clause in re.split(r"[;\n]|\bbut\b", words or "", flags=re.I))


def record_explicit_contact(target: str, per_day: int = 20) -> Grant:
    """Persist his standing instruction immediately, without earning ten approvals.

    The instruction comes from the turn boundary, never from a tool's arguments.
    This is one exact 1:1 contact and messaging only.
    """
    from . import capabilities, consent, scorecard
    said = scorecard.his_words(capabilities.said_this_turn())
    standing = re.compile(r"\b(?:going\s+forward|from\s+now\s+on|always|standing\s+permission)\b", re.I)
    verbs = re.compile(r"\b(?:message|send|reply|replies|talk|conversations?|follow[ -]?ups?)\b", re.I)
    clauses = re.split(r"[;\n]|\bbut\b", said, flags=re.I)
    if not any(standing.search(clause) and explicitly_names(clause, target) and any(
            not consent._negated(clause, m.start()) for m in verbs.finditer(clause)) for clause in clauses):
        raise ValueError("No explicit standing messaging instruction for this exact contact in your message")
    if any(c in target for c in (",", "+", ";")):
        raise ValueError("Standing contact permission is for one named person")
    limit = re.search(r"\b(\d+)\s+(?:messages?|times?)\s+(?:per|a|each)\s+day\b", said, re.I)
    if limit:
        per_day = int(limit.group(1))
    if int(per_day) < 1:
        raise ValueError("A zero daily limit does not grant permission; revoke the existing permission instead")
    return grant("send", target, per_day=max(1, min(int(per_day), 100)), words=said)


#: "for today", "just for today", "today only", "till evening", "until tonight".
_TODAY = re.compile(r"\b(?:(?:just\s+|only\s+)?for\s+today|today\s+only|for\s+the\s+day|"
                    r"(?:till|until)\s+(?:evening|tonight|eod|end\s+of\s+(?:the\s+)?day))\b", re.I)
#: He waives his approval: "without my approval", "don't need my approval",
#: "no need of my approval", "talk with her directly", "respond directly".
_WAIVER = re.compile(
    r"\b(?:without\s+(?:my\s+)?(?:approval|asking(?:\s+me)?|my\s+yes)|"
    r"(?:don'?t|do\s+not|dont|no)\s+(?:need|require[ds]?|required)?\s*(?:of\s+)?(?:me\s+|my\s+)?approval|"
    r"no\s+need\s+(?:of|for)\s+(?:my\s+)?approval|"
    r"(?:talk|respond|reply|answer|speak)\s+(?:with\s+(?:her|him|them)\s+)?directly)\b", re.I)


def _contact_named(words: str) -> list[str]:
    """His 1:1 contacts that `words` names by any one of their names."""
    from . import answers, chat_watch
    said = set(answers.name_tokens(words))
    # A person's chat is one where that person wrote: the 1:1 shows their name as
    # both the chat and the sender. Meeting chats never do — and their titles
    # ("… changes for billing") matched everyday words in his message (6 Nov,
    # found on live data after deploy: 14 candidates, so nothing was granted).
    with store._connect() as conn:
        people = [r[0] for r in conn.execute(
            "SELECT DISTINCT chat FROM teams_messages WHERE chat <> '' AND sender = chat").fetchall()]
    people = [c for c in people if not any(ch in c for ch in (",", "+")) and not chat_watch.is_from_him(c)]
    return [c for c in people
            if any(len(t) >= 3 and t in said for t in answers.name_tokens(c))]


def standing_from_words(words: str, now: float | None = None) -> Grant | None:
    """A permission he gave in so many words, for today, to one 1:1 contact.

    6 Nov: "Whatever Harika asks respond don't required me approval, talk with
    her directly just for today" got "noted" — and the next two replies were
    staged for his yes. The standing rule above wanted "going forward" and her
    full Teams name ("Nakka Harika"); and there was no way to say "for today".
    Read from HIS words at the turn boundary, never from a tool's arguments.
    One person (named by any of their names, unique among his chats), messaging
    only, ending at midnight. None when it is anything less clear than that.
    """
    said = " ".join((words or "").split())
    if not _TODAY.search(said) or not _WAIVER.search(said):
        return None
    # "don't reply to her without my approval" says the opposite of a waiver.
    if re.search(r"\b(?:don'?t|do\s+not|never)\s+(?:reply|respond|send|message|talk)\b", said, re.I):
        return None
    named = _contact_named(said)
    if len(named) != 1:
        return None
    day = time.localtime(time.time() if now is None else now)
    end = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 23, 59, 0, 0, 0, -1))
    return grant("send", named[0], per_day=50, words=said, expires_at=end)


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


def _contact_permission(target: str) -> tuple[bool, Grant | None]:
    """An explicit grant/revocation takes precedence over the legacy reply list."""
    with store._connect() as conn:
        rows = conn.execute("SELECT * FROM authority WHERE act='send' ORDER BY id DESC").fetchall()
    for row in rows:
        if row['target'].strip().casefold() == target.strip().casefold():
            g = _row(row)
            if row['active'] and not _live(row):
                continue            # "just for today" has ended: as if never given
            usable = bool(row['active']) and used_today(g.id) < g.per_day
            return True, g if usable else None
    return False, None


def auto_reply_to(target: str, group: bool = False) -> bool:
    """Exact 1:1 recipients covered by a saved grant or the legacy reply list."""
    if group or not target:
        return False
    from . import steward
    if steward.held_for(target):
        return False
    from . import guardrails, policy, senior
    if not policy.check("send", target).ok:
        return False
    recorded, permission = _contact_permission(target)
    if recorded:
        return permission is not None
    if senior.is_senior(target):
        return False
    names = [re.sub(r"^[-*]\s+", "", line.strip()).casefold()
             for line in guardrails.section("automatic teams replies").splitlines()
             if re.match(r"^\s*[-*]\s+", line)]
    return target.strip().casefold() in names


class ReplyNotAuthorized(PermissionError):
    """Reply preflight refused before invoking any transport."""


async def send_reply(target: str, text: str) -> str:
    """Send under the narrow standing rule, without recording a fresh approval."""
    if not auto_reply_to(target):
        raise ReplyNotAuthorized(f"No automatic Teams reply permission for {target}")
    recorded, permission = _contact_permission(target)
    if recorded and (permission is None or not claim_use(permission)):
        raise ReplyNotAuthorized("Permission revoked, held, or its daily limit is reached")
    from . import chat_watch, ops, teams_bridge
    token = teams_bridge._automatic_send.set(target)
    try:
        line = await ops.REGISTRY["teams_send"]["run"](to=target, text=text, to_group=False)
    finally:
        teams_bridge._automatic_send.reset(token)
    if not line.startswith("✅ Sent to "):
        raise RuntimeError(f"Teams send was not confirmed: {line}")
    chat_watch.note_asta_said(target, text)
    store.record_outcome("authority", "auto reply", subject=target[:80],
                         detail=text[:200])
    return line


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
    if to_group or g.act != "send" or g.target.strip().casefold() != to.strip().casefold():
        raise PermissionError("This permission covers only the exact 1:1 recipient")
    if not claim_use(g):
        raise PermissionError("Permission revoked, held, or its daily limit is reached")
    line = await ops.REGISTRY["teams_send"]["run"](to=to, text=what, to_group=to_group)
    if not line.startswith("✅ Sent to "):
        raise RuntimeError(f"Delivery unconfirmed: {line}")
    await notify.notify(f"📨 Sent to {to} under permission {g.id} "
                        f"({g.render()}):\n\n{what[:300]}\n\nSay “revoke {g.id}” to end it.",
                        "teams", urgency="direct", considered=True)
    return line


def claim_use(g: Grant, now: float | None = None) -> bool:
    """Reserve a permission use atomically, before yielding to a transport.

    Failed or interrupted sends retain their reservation: delivery can be
    uncertain. A fresh authorization check also honors revocation after staging.
    """
    from . import policy, steward
    if steward.held_for(g.target) or not policy.check(g.act, g.target).ok:
        return False
    key = _used_key(g.id, now)
    with store._connect() as conn:
        conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES (?, '0')", (key,))
        grant_row = conn.execute("SELECT per_day FROM authority WHERE id=? AND active=1", (g.id,)).fetchone()
        used = int(conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()["value"])
        if grant_row is None or used >= int(grant_row['per_day']):
            return False
        conn.execute("UPDATE kv SET value=? WHERE key=?", (str(used + 1), key))
    store.record_outcome("authority", "used", subject=str(g.id), detail="reserved before delivery")
    return True


def summary() -> str:
    live = grants()
    if not live:
        return "Asta has no standing permissions — everything outward waits for your yes."
    lines = [f"  {g.id}. {g.render()} (used {used_today(g.id)} today)" for g in live]
    return "Asta may do these without asking:\n" + "\n".join(lines) + "\nSay “revoke <n>” to end one."
