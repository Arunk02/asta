"""His manager and everyone above: nothing reaches them without his yes.

His rule, 1 Oct: "add a guardrail for one to one for my manager and above
layer, make sure messages go once I verified — even 'can I ask what is
needed'". Everywhere else Asta may acknowledge, ask back, check in, or send
what he asked for in his own words; to these people it may do none of that.
Every line is a draft he reads first.

The list is his, in guardrails.md under `## Manager and above`, one name per
bullet. Enforced in two places, so no path can skip it: the places that would
speak on their own stage a draft instead, and `teams_bridge.send_message`
refuses outright any send to them that does not carry his approval.
"""

from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar

SECTION = "manager and above"

#: Set only while a send he approved is running — his "send" to a staged draft,
#: or his approval of a drafted reply. Nothing else may reach these people.
APPROVED: ContextVar[bool] = ContextVar("asta_senior_approved", default=False)


def people() -> list[str]:
    """The names in his `## Manager and above` section, as written."""
    from . import guardrails
    body = guardrails.sections().get(SECTION, "")
    names = []
    for line in body.splitlines():
        m = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if not m:
            continue
        # "Praveen Kumar — my manager" → "Praveen Kumar"
        name = re.split(r"\s+[—–(-]\s*|\s*\(", m.group(1))[0].strip()
        if name:
            names.append(name)
    return names


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+", (text or "").lower())


def is_senior(target: str) -> bool:
    """Is this chat a 1:1 with someone on his list?

    Every word of a listed name must be in the chat's name, so "Praveen Kumar"
    matches the chat "Praveen Kumar S" but "Kumar" alone matches nobody. A group
    is never this — groups already wait for his yes."""
    target = (target or "").strip()
    if not target or "," in target:
        return False
    have = set(_tokens(target))
    for name in people():
        want = _tokens(name)
        if want and all(w in have for w in want):
            return True
    return False


class NeedsHisYes(RuntimeError):
    """A send to manager-and-above that he has not approved."""


def check(target: str) -> None:
    """Raise unless this send may go: not senior, or approved by him."""
    if is_senior(target) and not APPROVED.get():
        raise NeedsHisYes(
            f"🔒 {target} is on your manager-and-above list — nothing goes to them "
            f"without your yes. Stage it with prepare_to_send.")


@contextlib.contextmanager
def approved():
    """The block in which a send he approved runs."""
    token = APPROVED.set(True)
    try:
        yield
    finally:
        APPROVED.reset(token)
