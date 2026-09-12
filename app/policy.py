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

KINDS = ("mute", "never", "prefer", "note")


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
        return self.words[:160]


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
