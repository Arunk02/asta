"""What Arun asked for is already consented — and is never swapped for something else.

Two rules, one module, because they failed together on 27 August and the second is
only ever reached because of the first.

**Rule one: asking for an act IS the consent for it.** `teams_call` staged every
call and waited for a yes, including a call he had just asked for in words. Its own
docstring said "Only when Arun asked for a call" — so the precondition for reaching
it was the very consent the staging then asked for again. He put it plainly:

    "if i ask to call, then im aware right still what is the issue ?"

Nothing. Staging exists for acts Asta *proposes*, where the first he hears of it is
a colleague's phone ringing. For an act he named himself it is a second gate on a
door he already opened, and the cost is not politeness — it is that the call does
not happen.

**Rule two: an act Asta cannot perform is never replaced by a different one.** Asked
to call Vinish and talk through his review comments, Asta could not reach the call
tools (`tool_index` had not selected them), said so — and then dispatched a
background task to rewrite the code and push it:

    "I can't retract task #72 — no cancel/stop tool is available to me, and it's
     already running with instructions to push. It will push when done unless you
     intervene directly."

He asked for a conversation. He got seven code changes he never reviewed, heading
for a branch, plus himself named as the only way to stop it — on the same day he
said "i cant everytime come and fix".

The second rule is the one that matters. A refusal is cheap: he reads it and asks
again. A substitution spends his credibility with a colleague, or his repository,
on an act he did not choose, and he finds out afterwards. "Sometimes it can be
incorrect also" — and an incorrect act he never authorised is not recoverable by
telling him about it.

Both rules live here rather than in the tools they guard because every brain has to
agree about them. Per-brain copies of a policy are what put a 20-minute constant in
two places and made Teams and chat disagree about the same message.
"""

from __future__ import annotations

import re

from . import work_intent

#: A work verb reached through a conjunction: "call him AND fix the ETA".
#:
#: `work_intent.is_work_assignment` anchors its imperative at the start of the
#: message, which is right for its own job — deciding whether a message IS a work
#: assignment. Here the message opens with the conversation, so the code half
#: never leads, and asking for both would have had the code half refused. The verb
#: list is imported rather than retyped: two copies of it drifting apart is the
#: bug class that put a 20-minute constant in two places.
_ALSO_WORK = re.compile(
    rf"\b(?:and|then|also|plus|after\s+that|as\s+well\s+as|,)\s+"
    rf"(?:please\s+|pls\s+|can\s+you\s+)?(?:{work_intent._WORK_VERB})\b", re.I)

#: Asking someone to be rung. "call" alone is far too broad — a function call, an
#: API call, a close call — so a match must be the VERB with a person-shaped object.
#: Both sides are checked: "function call do" fails on the noun in front of it,
#: "call back later" on the particle behind it.
_CALL = re.compile(
    r"\b(?:call|ring|dial|phone)\s+(?:up\s+)?(?P<obj>[a-z][\w.'-]*)"
    r"|\b(?:give|place|make)\s+(?:him|her|them|[a-z][\w.'-]*)\s+a\s+(?P<obj2>call|ring)\b"
    r"|\b(?P<obj3>voice|phone)\s+call\b"
    r"|\bget\s+(?:him|her|them)\s+(?P<obj4>on)\s+the\s+phone\b",
    re.I)

#: Words that make "call" a noun rather than an instruction to ring somebody.
_CALL_NOUN_BEFORE = {"function", "api", "method", "service", "tool", "system", "close",
                     "conference", "sales", "wake", "http", "rpc", "the", "a", "an",
                     "this", "that", "one", "last", "first", "recursive", "async"}

#: Objects that are not a person. "call back", "call it off", "call the shots".
_CALL_NOT_A_PERSON = {"do", "does", "did", "back", "out", "off", "in", "on", "for",
                      "it", "this", "that", "they", "we", "us", "the", "a", "an",
                      "is", "are", "be", "been", "was", "were", "work", "works",
                      "happen", "happens", "fail", "fails", "return", "returns",
                      "stack", "site", "sign", "me", "myself", "again", "later",
                      "when", "if", "and", "but", "or", "to", "into", "up",
                      # "call you" is Arun addressing Asta, never a person to ring.
                      "you", "u", "ya", "yourself"}

#: Asking for a conversation with a human, by any channel. Deliberately wider than
#: _CALL: the substitution rule cares that he wanted a PERSON engaged, not which
#: wire carried it.
_TALK = re.compile(
    r"\b(?:discuss|talk|speak|chat|clarify|confirm|negotiate|sync|catch\s*up)\b"
    r"\s*(?:it|this|that|them)?\s*"
    r"(?:with|to)\b"
    r"|\b(?:ask|check|confirm|clarify|raise\s+it)\s+(?:with|to)\s+"
    r"(?:him|her|them|vinish|[a-z][\w.'-]*)"
    r"|\b(?:ask|ping|message|msg|dm|reply\s+to|respond\s+to|follow\s+up\s+with)\s+"
    r"(?:him|her|them|[a-z][\w.'-]*)"
    r"|\bget\s+(?:his|her|their)\s+(?:view|opinion|take|input|thoughts|confirmation)\b"
    , re.I)

#: Negations that turn "call him" into "don't call him". Checked against the words
#: immediately before the match, not the whole message — "don't push, discuss with
#: them" negates the push and asks for the discussion.
_NOT = re.compile(r"\b(?:don'?t|do\s+not|never|no\s+need\s+to|without|instead\s+of)\s*$", re.I)


def _negated(text: str, at: int) -> bool:
    return bool(_NOT.search(text[max(0, at - 24):at]))


def _is_person_call(text: str, m: re.Match) -> bool:
    """Is this "call" the verb, aimed at somebody?"""
    if m.group("obj") is None:
        return True                     # the other alternatives are already explicit
    before = re.findall(r"[\w'-]+", text[max(0, m.start() - 30):m.start()])
    if before and before[-1].lower() in _CALL_NOUN_BEFORE:
        return False
    return m.group("obj").lower() not in _CALL_NOT_A_PERSON


def _asked(pattern: re.Pattern, text: str, person: bool = False) -> bool:
    for m in pattern.finditer(text or ""):
        if _negated(text, m.start()):
            continue
        if person and not _is_person_call(text, m):
            continue
        return True
    return False


def asked_to_call(text: str) -> bool:
    """Did Arun, in these words, ask for someone to be rung?

    This is the whole of rule one. When it is true a call needs no confirmation,
    because the confirmation already happened — he typed it.
    """
    return _asked(_CALL, text or "", person=True)


def asked_to_talk(text: str) -> bool:
    """Did he ask for a PERSON to be engaged — called, asked, messaged, discussed with?"""
    return _asked(_TALK, text or "") or asked_to_call(text)


def substitution(turn_text: str, kind: str, repos: tuple[str, ...] = ()) -> str:
    """Why this background task replaces what he actually asked for, or "".

    Only `code` tasks are judged. An `analysis` spawn reads and reports; getting it
    wrong costs a paragraph. A `code` spawn edits a repository and pushes, and it
    is the one that ran on 27 August in place of a phone call.

    The test is deliberately narrow, because the cost is asymmetric in the other
    direction too: refusing a task he DID ask for wastes a turn and annoys him,
    which is nothing like a branch he never approved. So it holds only when he
    asked for a person and asked for no code:

        "call Vinish and discuss the comments"     -> blocked, he wanted a call
        "call Vinish, then fix the ETA validation" -> allowed, he asked for both
        "fix the ETA validation"                   -> allowed, no person named
    """
    if kind != "code" or not (turn_text or "").strip():
        return ""
    if not asked_to_talk(turn_text):
        return ""
    if work_intent.is_work_assignment(turn_text, repos) or _ALSO_WORK.search(turn_text):
        return ""                       # he asked for the conversation AND the work
    wanted = "call" if asked_to_call(turn_text) else "talk to them"
    return (f"You asked me to {wanted} — not to change code. I'm not spawning a code "
            f"task instead of doing what you asked; that is how seven edits you never "
            f"reviewed ended up heading for a branch on 27 August.\n\n"
            f"If the conversation tool isn't reachable this turn, say \"use the call "
            f"tools\" and I'll get them. If you do want the code changed as well, say "
            f"so and I'll spawn it alongside — not in place of.")
