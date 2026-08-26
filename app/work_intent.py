"""Is this message asking for code to be written?

Asta's own instruction already says "When Arun assigns work … delegate it as a
background task right away" and "Never plan or implement in chat yourself". The
model ignored both and implemented inline, and the chat turn's 300s budget killed
it mid-edit:

    RuntimeError: Copilot CLI turn timed out after 300s

An instruction the model may or may not follow is not a routing decision. This
module makes it one.

**Why routing here is safe even when it is wrong.** A code task does not change
anything on its own: it runs the context gate, plans, and STOPS for Arun's
approval. So a false positive costs him a plan he did not want and can decline.
The failure it replaces is a half-finished edit and a five-minute wait ending in a
stack trace. Those are not comparable, which is what makes an imperfect
classifier the right tool.

The bias is therefore deliberate and one-directional: **miss rather than
over-claim on anything question-shaped**, because "how does X work" answered as a
plan is annoying in a way that erodes trust, while "implement X" answered in chat
is the bug being fixed.
"""

from __future__ import annotations

import re

#: Verbs that mean "change the code". Present-tense imperatives only — the past
#: and progressive forms almost always appear in questions and reports ("who
#: implemented this", "it keeps failing"), so they are not listed.
_WORK_VERB = (
    r"implement|fix|add|change|modify|edit|refactor|rename|migrate|remove|delete|"
    r"drop|update|upgrade|revert|introduce|extend|replace|correct|handle|support|"
    r"wire|hook|expose|validate|guard|bump")

#: An imperative opening: the verb leads, optionally after a polite prefix.
_IMPERATIVE = re.compile(
    rf"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|pls\s+|kindly\s+)?(?:{_WORK_VERB})\b",
    re.I)

#: A ticket key — BEPTELIKOS-10159, ABC-1. Strong evidence of assigned work when
#: it appears with a work verb anywhere in the sentence.
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,14}-\d+\b")
_WORK_VERB_ANYWHERE = re.compile(rf"\b(?:{_WORK_VERB})\b", re.I)

#: Question shapes. Any of these and the message is not a work assignment,
#: whatever else it contains — "how do I fix this" is a question about fixing.
_QUESTION = re.compile(
    r"^\s*(what|which|who|whose|when|where|why|how|is|are|was|were|do|does|did|"
    r"has|have|had|should|would|shall|any|can\s+i|could\s+i|explain|tell|show|"
    r"describe|list|find|search|check|look|read|review|analyse|analyze|trace|"
    r"debug|investigate)\b", re.I)

#: Phrases that mean "talk about it", not "do it".
_DISCUSS = re.compile(
    r"\b(how (would|do|does|should|can) (you|i|we)|what (would|do) you think|"
    r"do you think|opinion|suggest|advice|explain|walk me through|"
    r"is it possible|can it be|would it be)\b", re.I)

#: Evidence the thing being changed is CODE.
#:
#: A work verb on its own is nowhere near enough, and finding that out is what
#: this section exists for. "update me on the PR", "change my status to busy",
#: "drop the call", "delete that message", "add me to the review" all lead with a
#: listed verb and none of them is code work — each belongs to a different flow
#: that would have been hijacked. Requiring positive evidence turns the default
#: from "route unless it looks like a question" into "route only when it is
#: recognisably about code", which is the direction the bias is supposed to run.
_CODE_NOUN = re.compile(
    r"\b(test|tests|class|classes|method|function|endpoint|api|field|column|"
    r"schema|migration|mapper|dto|entity|repository|controller|service|handler|"
    r"activity|workflow|validation|validator|guard|check|null|exception|error "
    r"handling|logic|flag|config|configuration|constant|enum|import|imports|"
    r"dependency|version|bug|regression|code|refactor|unit test|integration test|"
    r"logging|log line|retry|timeout|cache|query|sql|branch|pom|yml|yaml|json|"
    r"java|python|typescript|\.java|\.py|\.ts|\.yml|\.xml)\b", re.I)

#: Objects that belong to OTHER flows. Naming one of these means the message is
#: about a message, a call, a meeting or a ticket field — not about source code —
#: however work-like the verb is.
_OTHER_FLOW = re.compile(
    r"\b(status|presence|call|meeting|invite|calendar|message|msg|chat|thread|"
    r"mail|email|reply|dm|group|leave|ooo|reminder|standup|brief|me\b)\b", re.I)


def is_work_assignment(text: str, repos: tuple[str, ...] = ()) -> bool:
    """True when this is Arun handing over CODE work, not asking about it.

    Deliberately narrow, and narrow in one direction: everything ambiguous
    returns False and takes the old chat path — which still works, and which can
    no longer edit files, so the model has to delegate from there anyway. A
    missed route costs a slower path; a wrong route spawns work he did not ask
    for, and that is the one that erodes trust.
    """
    t = (text or "").strip()
    if not t or len(t) > 600:
        return False
    if t.endswith("?"):
        return False
    if _QUESTION.match(t) or _DISCUSS.search(t):
        return False

    ticket = bool(_TICKET.search(t))
    # A ticket key is evidence on its own — it names a unit of tracked work — and
    # overrides the other-flow guard, since "comment on PROJ-1 and fix the NPE"
    # is still code work.
    if not ticket and _OTHER_FLOW.search(t):
        return False

    named_repo = any(r and r.lower() in t.lower() for r in repos)
    has_code_evidence = ticket or named_repo or bool(_CODE_NOUN.search(t))
    if not has_code_evidence:
        return False

    if _IMPERATIVE.match(t):
        return True
    return bool(ticket and _WORK_VERB_ANYWHERE.search(t))


def ticket_in(text: str) -> str:
    m = _TICKET.search(text or "")
    return m.group(0) if m else ""


def title_for(text: str, limit: int = 70) -> str:
    """A task title from his own words, so he recognises it in a list."""
    t = " ".join((text or "").split())
    ticket = ticket_in(t)
    if ticket and not t.upper().startswith(ticket):
        t = f"{ticket}: {t}"
    return t[:limit]
