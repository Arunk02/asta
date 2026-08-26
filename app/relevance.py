"""Intent-type gate — the objective, zero-token guard against a passive question
silently spawning a side-effecting action.

The failure it closes: Arun asked "No recent one..?" (a question about whether
Vinish had messaged) and Asta went and ran a repo analysis on an unrelated project
— it answered a question he never asked and *acted* on it. That is intent drift.

The insight that makes this cheap and durable: don't judge whether an answer is
GOOD (fuzzy, costs tokens, can be flattered). Judge whether a *question* was
allowed to trigger an *action*. A question → side-effecting-action transition is a
structural mismatch — as objective as an exit code — so it can gate. Anything
fuzzier (semantic relevance) only ever *measures*, never blocks.

Design rules, same contract as `verify.py`:

  - **Off by default.** `ASTA_RELEVANCE=1` opts in; prod is byte-identical until then.
  - **Command beats question.** A message that names the work ("fix X", "analyse the
    repo", "yes go ahead") is a command even when phrased as a question ("can you
    fix X?") — so the gate never blocks a real request for work.
  - **Only the danger zone blocks.** A question / terse follow-up carrying NO action
    verb is the only thing held. A plain statement is left alone (default allow) so
    the gate stays a scalpel, not a net.
  - **No trigger → no change.** When there is no user message behind the spawn (the
    autonomous loop, an offer Arun already accepted), the gate is a pure no-op.
"""

from __future__ import annotations

import contextvars
import os
import re

_TRUEY = ("1", "true", "yes", "on")

#: The message that opened the current turn, so a tool spawned mid-turn can see what
#: Arun actually said — bound in `_run_turn`, read inside `delegate_task`. A
#: contextvar (not an argument) for the same reason `tasks._TURN_CONV` is one: the
#: model must not have to fill it, and it must not leak across concurrent turns.
_TRIGGER: contextvars.ContextVar[str] = contextvars.ContextVar("turn_trigger", default="")

#: kinds `delegate_task` can spawn that DO work in response to the turn. teams_draft
#: is deliberately absent: it already waits for Arun's explicit approval before
#: anything leaves, so gating it again would only nag.
GUARDED_KINDS = ("code", "analysis")

# Imperative work verbs and go-ahead phrases. Their presence means the message is
# asking for work — the one case where auto-spawning is unambiguously wanted. Kept
# broad on purpose: a false "this is a command" only lets a spawn through (today's
# behaviour), while a false "this is passive" would nag, so we bias toward command.
_ACTION = re.compile(
    r"""\b(
        fix|implement|add|analyse|analyze|review|refactor|rewrite|write|create|
        build|run|investigate|debug|diagnose|update|remove|delete|drop|migrate|
        ship|raise|open|merge|rebase|bump|patch|wire|port|deploy|configure|
        generate|check|test|look\s+into|dig\s+into|go\s+(?:ahead|look|and)|
        do\s+it|get\s+it\s+done|take\s+care\s+of|make\s+it|please\s+do|carry\s+on
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

# A message that leads with an interrogative, or is a bare affirmative/negative — the
# shape of a thing that wants an ANSWER, not work.
_QUESTION_LEAD = re.compile(
    r"""^\s*(
        is|are|was|were|am|do|does|did|can|could|will|would|should|has|have|had|
        any|anything|anyone|what|whats|which|who|whom|whose|where|when|why|how
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

# Deictic / elliptical markers that only make sense as a follow-up to the previous
# turn — "no recent one", "the second one", "and?", "any newer". These carry no work
# of their own; acting on one is guessing.
_DEICTIC = re.compile(
    r"\b(no|none|nothing|nope|nah|recent|latest|newer|older|newest|oldest|"
    r"that\s+one|this\s+one|the\s+(?:first|second|third|last|other)|"
    r"which\s+one|and|then|so|really|sure|ok|okay)\b",
    re.IGNORECASE,
)


def enabled() -> bool:
    """Off by default. The gate only engages when this is explicitly set."""
    return os.environ.get("ASTA_RELEVANCE", "").strip().lower() in _TRUEY


def bind_trigger(text: str) -> None:
    """Record the message that opened this turn (called from the chat layer)."""
    _TRIGGER.set((text or "").strip())


def current_trigger() -> str:
    return _TRIGGER.get()


def is_command(text: str) -> bool:
    """True when the message names work to do — an action verb or a go-ahead."""
    return bool(_ACTION.search(text or ""))


def is_question(text: str) -> bool:
    """True when the message wants an answer: it ends with '?' or leads interrogative."""
    t = (text or "").strip()
    return bool(t) and (t.endswith("?") or _QUESTION_LEAD.match(t) is not None)


def is_terse_followup(text: str) -> bool:
    """A short, deictic message that only means something against the prior turn."""
    t = (text or "").strip()
    if not t:
        return False
    words = t.split()
    return len(words) <= 6 and _DEICTIC.search(t) is not None


def passive(text: str) -> bool:
    """The danger zone: a question or terse follow-up that does NOT ask for work.

    A command (even one phrased as a question, "can you fix X?") is never passive —
    command detection wins. A plain declarative that is neither a command nor a
    question defaults to NOT passive, so the gate holds only what it is sure about.
    """
    t = (text or "").strip()
    if not t:
        return False
    if is_command(t):
        return False
    return is_question(t) or is_terse_followup(t)


def guard_spawn(kind: str, title: str = "", workspace: str = "") -> str | None:
    """The gate, evaluated at the spawn chokepoint. Returns a one-line confirm to
    show Arun (and records the catch) when a passive turn is about to spawn work;
    returns None — spawn as usual — in every other case.

    Non-blocking by construction: disabled, an unguarded kind, no user trigger, or a
    trigger that actually asked for work all return None. Only a real
    question-then-action mismatch is held.

    Anchor drift (a spawn aimed at a workspace the turn inherited silently rather
    than the one Arun named) is *measured* here, never blocked — it fires even for a
    genuine command, because the risk it guards is the wrong TARGET, not the wrong
    intent. Objective detection, measure-only action: the disciplined split.
    """
    if not enabled():
        return None
    if kind not in GUARDED_KINDS:
        return None
    _note_target_drift(kind, title, workspace)
    trigger = current_trigger()
    if not trigger:                      # autonomous / accepted-offer spawn — no drift to catch
        return None
    if not passive(trigger):             # he asked for work — let it run
        return None
    _record("held", title, f"{kind}: {trigger[:120]}")
    verb = "look into that" if kind == "analysis" else "make that change"
    return (f"You asked a question, so I held off before spawning a {kind} task"
            + (f" (“{title}”)" if title else "")
            + f". Want me to actually go {verb}? Reply “yes, go ahead” and I'll run it.")


# --- anchor drift: was this work aimed at a workspace nobody named? --------------
#
# The second half of the incident: the chat had silently inherited the
# contmark-agent-harness workspace from an earlier offer, so work spawned into the
# WRONG repo even when the ask itself was fine. We stamp *how* a conversation's
# workspace was set — inherited (adopted from an offer/task) vs explicit (Arun
# picked it) — then flag a spawn into an inherited workspace that the ask never
# mentioned. Structural and cheap; measure-only, so it can never strand real work.

def _inherited_key(cid: str) -> str:
    return f"conv_ws_inherited:{cid}"


def mark_inherited_workspace(cid: str, workspace: str) -> None:
    """The conversation adopted this workspace from an offer/task, not from Arun."""
    if not cid or not workspace:
        return
    with _suppress():
        from . import store
        store.kv_set(_inherited_key(cid), workspace)


def clear_inherited_workspace(cid: str) -> None:
    """Arun set the workspace explicitly — no longer an inherited anchor."""
    if not cid:
        return
    with _suppress():
        from . import store
        store.kv_set(_inherited_key(cid), "")


def inherited_workspace(cid: str) -> str:
    if not cid:
        return ""
    with _suppress():
        from . import store
        return store.kv_get(_inherited_key(cid)) or ""
    return ""


def _names_workspace(haystack: str, workspace: str) -> bool:
    """True when the ask/title actually refers to that workspace — any of its
    word-parts (contmark / agent / harness) appearing is enough to call it named."""
    hay = (haystack or "").lower()
    return any(tok for tok in re.split(r"[^a-z0-9]+", workspace.lower())
               if len(tok) >= 3 and tok in hay)


def _note_target_drift(kind: str, title: str, workspace: str) -> None:
    from . import tasks
    cid = tasks.current_conversation()
    inherited = inherited_workspace(cid) if cid else ""
    ws = (workspace or "").strip()
    # only interesting when the spawn targets the very workspace we inherited silently
    if not inherited or ws != inherited:
        return
    if _names_workspace(f"{current_trigger()} {title}", inherited):
        return                           # the ask referred to it — not drift
    _record("drift", title, f"{kind} → inherited ws '{inherited}' unnamed: {current_trigger()[:100]}")


# --- semantic tier: did the ANSWER address the question? (measure-only) ----------
#
# The intent gate stops a question from spawning work; this catches the quieter
# case — a question answered off-topic with no task at all. Fuzzy, so it never
# blocks: it only records. Two-stage for cost — a free word-overlap pre-filter
# settles the common case at zero tokens, and only a low-overlap answer spends one
# tiny local-model yes/no. Local model down → skip, exactly like a missing oracle.

_STOP = frozenset(
    "the a an and or but is are was were be been being to of in on for from with "
    "you your yours it its this that these those i me my we our he she they them "
    "his her their what which who whom whose any anything did does do have has had "
    "will would can could should about here there just now then so no not".split())

#: fraction of the question's salient terms that must reappear in the answer to call
#: it on-topic for free; below this we spend the one cheap confirming call.
_OVERLAP_OK = 0.5


def _salient(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower()) if w not in _STOP}


def _overlap(question: str, answer: str) -> float:
    q = _salient(question)
    if not q:
        return 1.0                       # nothing distinctive to miss
    return len(q & _salient(answer)) / len(q)


async def judge_answer(question: str, answer: str) -> None:
    """Record whether a reply addressed the question it answered. Never raises,
    never blocks, and usually costs nothing (the overlap pre-filter)."""
    try:
        if not enabled():
            return
        q, a = (question or "").strip(), (answer or "").strip()
        if not q or not a or not is_question(q):
            return
        if _overlap(q, a) >= _OVERLAP_OK:
            return                       # clearly on-topic — the free, common case
        verdict = await _local_addresses(q, a)
        if verdict is None:              # local model unavailable — skip, don't guess
            return
        _record("ontopic" if verdict else "offtopic", "", q[:120])
    except Exception:
        pass


async def _local_addresses(question: str, answer: str) -> bool | None:
    """One tiny local yes/no. None when the local model can't be reached."""
    import asyncio

    from . import memory
    prompt = (f"A user asked: {question}\n\nThe assistant replied: {answer[:800]}\n\n"
              f"Does the reply directly address what the user asked? Answer only yes or no.")
    out = await asyncio.to_thread(memory.local_llm_complete, prompt, 5)
    if not out:
        return None
    return out.strip().lower().startswith("y")


# --- shared recorder ------------------------------------------------------------

def _record(outcome: str, subject: str, detail: str) -> None:
    """Count a signal so drift becomes a number, not an anecdote. Never let
    measurement break the thing measured."""
    with _suppress():
        from . import store
        store.record_outcome("relevance", outcome, subject=(subject or "")[:80], detail=detail)


def _suppress():
    import contextlib
    return contextlib.suppress(Exception)
