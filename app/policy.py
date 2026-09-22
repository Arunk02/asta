"""The policy gate: his standing rules, checked by code before anything acts.

R3 of the Astra-class plan, in his words: "if i gave some instructions it is
not following". A correction used to become a memory fact the CHAT brain might
read. The responder, the watchers, the ops that send and the task legs never
looked — so "don't check on any incidents going forward" held in one place and
was ignored in the next, and he had to say it again.

A rule here is data with a kind:

    mute     stop investigating a kind of ask          act=investigate  target=incident
    never    do not do this act (to this target)       act=send         target=<person>
    prefer   a default he has stated                   act=workspace    value=booking
    note     a standing instruction for the brains     (routed through guardrails.md)
    quiet    nothing reaches his phone for a while      act=push  value=days=sat,sun;release=mon 09:00

and `check(act, target)` is the one question every doer asks before acting.
"Unless I ask" is part of the rule: `asked=True` — he requested this act in so
many words, this turn — lets it through.

Rules are written only with his yes (app/instructions.py stages them as an
offer), so nothing here can quietly start refusing work he never ruled out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import store

KINDS = ("mute", "never", "prefer", "note", "quiet")


@dataclass(frozen=True)
class Rule:
    id: int
    kind: str
    act: str = ""
    target: str = ""
    value: str = ""
    unless_asked: bool = False
    words: str = ""

    def render(self) -> str:
        """One line he can read and recognise as his rule."""
        tail = " unless you ask" if self.unless_asked else ""
        if self.kind == "mute":
            return f"Don't investigate {self.target or 'anything'} asks{tail}"
        if self.kind == "never":
            who = f" {self.target}" if self.target else ""
            verb = {"send": "Never message", "call": "Never call", "push": "Never push",
                    "comment": "Never comment on", "merge": "Never merge"}.get(self.act,
                                                                            f"Never {self.act}")
            return f"{verb}{who}{tail}"
        if self.kind == "prefer":
            return f"Default {self.act}: {self.value}"
        if self.kind == "quiet":
            return describe_quiet(self.value)
        # Cut at a word, with a mark that it was cut. [:160] ended his standup
        # rule on "rememb" in the one message whose whole job is to show him the
        # rule he is agreeing to.
        from .clip import clip
        return clip(self.words, 240)


@dataclass(frozen=True)
class Decision:
    ok: bool
    rule: Rule | None = None

    @property
    def why(self) -> str:
        return f"your standing rule: “{self.rule.render()}”" if self.rule else ""


def _row(r) -> Rule:
    return Rule(id=r["id"], kind=r["kind"], act=r["act"], target=r["target"],
                value=r["value"], unless_asked=bool(r["unless_asked"]), words=r["words"])


def rules(kind: str = "") -> list[Rule]:
    with store._connect() as conn:
        q = "SELECT * FROM rules WHERE active=1" + (" AND kind=?" if kind else "") + " ORDER BY id"
        rows = conn.execute(q, (kind,) if kind else ()).fetchall()
    return [_row(r) for r in rows]


def add(kind: str, act: str = "", target: str = "", value: str = "",
        unless_asked: bool = False, words: str = "") -> Rule:
    """Record a rule he approved. The same rule twice is one rule."""
    if kind not in KINDS:
        raise ValueError(f"rule kind must be one of {', '.join(KINDS)}")
    act, target = (act or "").strip().lower(), (target or "").strip()
    for r in rules(kind):
        if r.act == act and r.target.lower() == target.lower() and r.value == value:
            return r
    if kind == "prefer":
        # A new default replaces the old one rather than competing with it.
        with store._connect() as conn:
            conn.execute("UPDATE rules SET active=0 WHERE kind='prefer' AND act=?", (act,))
    with store._connect() as conn:
        cur = conn.execute(
            "INSERT INTO rules (kind, act, target, value, unless_asked, words, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (kind, act, target, value, int(unless_asked), (words or "")[:500], time.time()))
        rid = cur.lastrowid
    if kind == "mute" and target:
        # The responder's own mute list is where its fast path looks; keep one truth.
        from . import responder
        responder.mute(target)
    store.record_outcome("rule", "added", subject=str(rid), detail=f"{kind} {act} {target}"[:200])
    return next(r for r in rules(kind) if r.id == rid)


def drop(rule_id: int) -> bool:
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM rules WHERE id=? AND active=1", (rule_id,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE rules SET active=0 WHERE id=?", (rule_id,))
    r = _row(row)
    if r.kind == "mute" and r.target:
        from . import responder
        responder.unmute(r.target)
    return True


def _matches(rule_target: str, target: str) -> bool:
    if not rule_target:
        return True
    a, b = rule_target.lower(), (target or "").lower()
    return bool(b) and (a in b or b in a)


def check(act: str, target: str = "", asked: bool = False) -> Decision:
    """May Asta do `act` (to `target`) now? The first rule that forbids it says why."""
    act = (act or "").lower()
    for r in rules():
        if r.kind == "mute" and act == "investigate" and _matches(r.target, target):
            if not (asked and r.unless_asked):
                return Decision(False, r)
        elif r.kind == "never" and r.act == act and _matches(r.target, target):
            if not (asked and r.unless_asked):
                return Decision(False, r)
    return Decision(True)


def prefer(act: str) -> str:
    """A default he stated ("my favourite workspace is booking"), or ''."""
    for r in reversed(rules("prefer")):
        if r.act == (act or "").lower():
            return r.value
    return ""


def adopt_legacy() -> list[Rule]:
    """Mutes he gave before rules existed (the responder's own list) become rules,
    so "my rules" shows everything that is actually being enforced."""
    from . import responder
    have = {r.target for r in rules("mute")}
    out = []
    for kind in sorted(responder.muted_kinds() - have):
        out.append(add("mute", "investigate", kind, unless_asked=True,
                       words=f"(given before rules existed) don't investigate {kind} asks"))
    return out


def summary() -> str:
    live = rules()
    if not live:
        return "No standing rules yet."
    return "Your standing rules:\n" + "\n".join(f"  {r.id}. {r.render()}" for r in live)


# --- quiet time ---------------------------------------------------------------------
#
# 19 Sep: "sat and Sunday be on silent … summarise all on Monday mrng". There was
# no kind of rule that could hold a schedule, so it became a memory note that
# nothing sending a push ever read. A quiet rule is read at the one door every
# push goes through (app/notify.py): while it holds, nothing reaches his phone
# and everything is kept; when it ends, it all arrives as one summary.
#
# value, as data: "days=sat,sun;release=mon 09:00" (every week) or
# "from=<epoch>;until=<epoch>" (once). Local time — the clock he is speaking in.

import datetime as _dt

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_quiet(value: str) -> dict:
    out: dict = {}
    for part in (value or "").split(";"):
        key, _, val = part.partition("=")
        key, val = key.strip(), val.strip()
        if key == "days":
            out["days"] = [_WEEKDAYS.index(d) for d in val.split(",") if d.strip() in _WEEKDAYS]
        elif key == "release":
            day, _, hhmm = val.rpartition(" ") if " " in val else ("", "", val)
            h, _, m = hhmm.partition(":")
            out["release"] = (_WEEKDAYS.index(day) if day in _WEEKDAYS else None,
                              int(h or 9), int(m or 0))
        elif key in ("from", "until"):
            out[key] = float(val)
    return out


def describe_quiet(value: str) -> str:
    q = parse_quiet(value)
    if q.get("days"):
        names = [("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays",
                  "Saturdays", "Sundays")[d] for d in q["days"]]
        when = " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " and " + names[-1]
        line = f"Quiet on {when}"
    elif q.get("until"):
        fmt = "%a %d %b %H:%M"
        line = "Quiet until " + _dt.datetime.fromtimestamp(q["until"]).strftime(fmt)
        if q.get("from", 0) > time.time() + 60:
            line = ("Quiet from " + _dt.datetime.fromtimestamp(q["from"]).strftime(fmt)
                    + line[len("Quiet"):])
        return line + " — then one summary of what came in"
    else:
        line = "Quiet"
    rel = q.get("release")
    if rel:
        day = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
               "Sunday")[rel[0]] + " " if rel[0] is not None else ""
        line += f" — one summary {day}{rel[1]:02d}:{rel[2]:02d}"
    return line


def _quiet_holds(q: dict, now: float, since: float = 0.0) -> bool:
    if "until" in q:
        return q.get("from", since) <= now < q["until"]
    days = q.get("days") or []
    if not days:
        return False
    at = _dt.datetime.fromtimestamp(now)
    if at.weekday() in days:
        return True
    # "…summarise on Monday morning": the quiet runs on to the release, so
    # Sunday night's pushes do not all fire at 00:01 on Monday.
    rel = q.get("release")
    if rel and rel[0] == at.weekday() and (at.weekday() - 1) % 7 in days:
        return (at.hour, at.minute) < (rel[1], rel[2])
    return False


LIFTED_KEY = "quiet_lifted_until"


def quiet_holding(now: float | None = None):
    """The quiet rule in force right now, or None."""
    now = time.time() if now is None else now
    try:
        if float(store.kv_get(LIFTED_KEY) or 0) > now:
            return None                     # "I'm back" — for the rest of this stretch
    except ValueError:
        pass
    for r in rules("quiet"):
        if _quiet_holds(parse_quiet(r.value), now):
            return r
    return None


def quiet_ends(rule: Rule, now: float | None = None) -> float:
    """When this stretch of quiet stops holding."""
    now = time.time() if now is None else now
    q = parse_quiet(rule.value)
    if "until" in q:
        return q["until"]
    at = now
    while at < now + 8 * 86400 and _quiet_holds(q, at):
        at += 900
    return at


def lift_quiet(now: float | None = None):
    """He is back before his quiet time ends: lift it until it would have ended.
    A weekly rule stays his rule — only this stretch of it is lifted."""
    now = time.time() if now is None else now
    rule = quiet_holding(now)
    if rule is None:
        return None
    store.kv_set(LIFTED_KEY, str(quiet_ends(rule, now)))
    return rule


def expire_quiet(now: float | None = None) -> int:
    """A one-off quiet window that has ended is no longer a rule he has — it
    would sit in "my rules" for ever. Weekly ones never expire."""
    now = time.time() if now is None else now
    n = 0
    for r in rules("quiet"):
        until = parse_quiet(r.value).get("until")
        if until and now >= until:
            drop(r.id)
            n += 1
    return n
