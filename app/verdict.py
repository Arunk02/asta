"""What an exact root cause looks like, and how to tell a guess from one.

17 Nov, two cases from one colleague, five wrong answers between them, every one said
with confidence: "VTS showing pending is just a display lag on their side",
"no gating — a validation PR isn't in SIT", "doc-only save runs AP's amend flow, no skip
check", "the amend request doesn't carry the full reference list". None of them
quoted the log line and the code line that would have proved it, and the right
answers (dates present → appliedDates gate; ocean mapper diffs every reference
and VESSEL_ATA was new) were each one payload and one `if` away.

One definition, used in three places: the recipe a case turn carries
(booking_case.rule), the check on a draft before he sees it (answers.render),
and the hold on a cause sent to a colleague he did not ask to send
(agent.teams_send_message).
"""
from __future__ import annotations

import re

#: The answer, in this shape and nothing wider. The recipe hands it to the brain.
SHAPE = (
    "Summarise SIMPLY, the answer to their question first: Why — ONE cause, with the "
    "Class.java:line and condition that decided it · Timeline — the log lines that "
    "prove it (UTC, service, exact words) · Not confirmed · Next step. End "
    "'Confidence: CONFIRMED' only if you read both this turn — the log line being the "
    "one where the system logged its decision — else NOT CONFIRMED with "
    "the one check that settles it. Never two causes or 'likely'.\n")

#: A question about why something happened although something else said it should not.
WHY_METHOD = (
    "It is a WHY question. Work it like this, in order:\n"
    "1. Find the code that decides it — the gate or the condition that lets X happen "
    "(resolve_context, then Read/Grep in the registered workspace).\n"
    "2. Read the condition exactly: which fields or values does it compare?\n"
    "3. Fetch the values it saw for THIS case — the payload or request line whole "
    "(log_lines full=True), the stored state before it, and the line where the system "
    "logged what it DECIDED ('resolved SUCCESS/FAILED', 'published', 'on hold'). A status "
    "field is not the outcome. You may read any code in the registered workspace and any "
    "log or workflow you need — keep going until that decision line is read.\n"
    "4. Compare them field by field. The field that differs, or the value the condition "
    "read, is the root cause. A field nobody displays still counts.\n"
    "5. Say which it is: BY DESIGN (the condition did what it says, quote it) or A BUG "
    "(it did not). Once you have read the condition, 'intended or not' is never left "
    "open as 'not confirmed'.\n")

HEDGE = re.compile(
    r"\b(?:most\s+)?likely\b|\bprobably\b|\bpossibly\b|\bcould\s+be\b|\bmight\s+be\b|"
    r"\bmay\s+be\s+(?:due|because|caused)\b|\blooks\s+like\b|\bseems?\s+(?:like|to)\b|"
    r"\beither\b[^.\n]{1,80}\bor\b|\bdisplay\s+lag\b|\bnot\s+sure\b|\bI\s+think\b", re.I)

CAUSAL = re.compile(
    r"\broot\s*cause\b|\bbecause\b|\bdue\s+to\b|\bcaused\s+by\b|\bthat'?s\s+why\b|"
    r"\bthat\s+is\s+why\b|\bwhich\s+is\s+why\b|\bthat'?s\s+the\s+reason\b|"
    r"\bso\s+it\s+(?:went|proceeded|fired|was\s+sent)\b|\bthe\s+reason\b|\bissue\s+(?:is|was)\b|\bbug\s+(?:is|was)\b|"
    r"\bwhich\s+(?:is\s+what\s+)?(?:fires|triggers|caused)\b", re.I)

#: A code citation: Class.java:123, Class.method:123, or a file:line.
CODE = re.compile(r"\b[A-Za-z_][\w$]*(?:\.java|\.kt|\.py|\.\w+\(\))?:\d{1,5}\b")
#: A log citation: a clock time to the second.
LOG = re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b")
CONFIRMED = re.compile(r"Confidence:\s*CONFIRMED\b", re.I)


def problems(analysis: str, why: bool = False) -> list[str]:
    """Why this is not yet an exact root cause. Empty when it is one."""
    text = analysis or ""
    found = []
    hedge = HEDGE.search(text)
    if hedge:
        found.append(f"it guesses (“{hedge.group(0)}”)")
    if CAUSAL.search(text) or why:
        if not CODE.search(text):
            found.append("no code line shows what decided it")
        if not LOG.search(text):
            found.append("no log line with its time shows what happened")
    return found


def confirmed(analysis: str) -> bool:
    """Says CONFIRMED, and carries the proofs that make it so."""
    return bool(CONFIRMED.search(analysis or "")) and not problems(analysis, why=True)


def states_a_cause(text: str) -> bool:
    return bool(CAUSAL.search(text or ""))


#: "Confidence: CONFIRMED", "**Confidence:** CONFIRMED", "**Confidence**: CONFIRMED".
_SAYS_CONFIRMED = re.compile(r"Confidence\**:\**\s*CONFIRMED\b", re.I)
#: The same three ways of writing the "Not confirmed" label (18 Nov, live:
#: "**Not confirmed**:" slipped past a pattern that knew only the other two).
_NOT_CONFIRMED_PART = re.compile(
    r"Not confirmed\**:\**\s*(.+?)(?=\n\s*\n|\n\W*(?:Next step|Confidence|Timeline|Why)\b|\Z)",
    re.I | re.S)
#: "nothing", and "nothing — read both the test and the formatter this turn"
#: (18 Nov, live: the reason after the dash was taken for a gap, a false alarm
#: on an honest answer). "None of the payload was read" stays a gap.
_NOTHING = re.compile(r"^\W*(?:(?:nothing|none|n/?a)\s*(?:$|[—–\-:;,.(])|-+\W*$)", re.I)


def overclaims(answer: str) -> str:
    """What the answer itself says it did not check, when it still ends CONFIRMED.

    18 Nov, live: "Not confirmed: I didn't pull booking-service's status line"
    and then "Confidence: CONFIRMED" — the line it skipped was the one that
    decided the case, and the mechanism it confirmed was wrong."""
    if not _SAYS_CONFIRMED.search(answer or ""):
        return ""
    m = _NOT_CONFIRMED_PART.search(answer or "")
    if not m:
        return ""
    gap = " ".join(m.group(1).replace("*", "").split())
    return "" if _NOTHING.match(gap) else gap[:220]
