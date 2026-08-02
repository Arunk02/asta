"""The intent-type gate — a question must not silently spawn work.

Grounded in the real incident: "No recent one..?" (a question about whether Vinish
had messaged) caused Asta to run a repo analysis. These lock in that the gate holds
exactly that shape and — just as important — never nags a genuine request for work.
"""

from __future__ import annotations

import contextvars

import pytest

from app import memory, relevance, store, tasks


# --- classification: commands (must ALWAYS be allowed to spawn) ----------------

COMMANDS = [
    "fix the login bug",
    "implement the retry logic",
    "analyse the contmark-agent-harness repo",
    "review PR #1337",
    "refactor the dispatch path",
    "can you fix the failing test?",          # question-shaped, but names the work
    "could you add a timeout here?",
    "go ahead and open the PR",
    "yes, go ahead",
    "do it",
    "please do that",
    "run the suite and tell me",
    "dig into why CI is red",
    "look into the flaky test",
    "carry on",
]


@pytest.mark.parametrize("text", COMMANDS)
def test_a_request_for_work_is_a_command_never_passive(text):
    assert relevance.is_command(text) is True
    assert relevance.passive(text) is False


# --- classification: passive (the danger zone the gate holds) ------------------

PASSIVE = [
    "No recent one..?",                       # the real incident
    "Any message from Vinish?",
    "anything from him?",
    "no newer one?",
    "which one?",
    "the second one?",
    "and?",
    "is there anything else?",
    "did he reply?",
    "what did she say?",
    "nothing since then?",
]


@pytest.mark.parametrize("text", PASSIVE)
def test_a_question_with_no_work_is_passive(text):
    assert relevance.is_command(text) is False
    assert relevance.passive(text) is True


# --- classification: plain statements default to NOT passive (scalpel, not net) -

NEUTRAL = [
    "the login flow is broken",               # a statement — left alone, not held
    "CI has been flaky lately",
    "thanks, that's helpful",
]


@pytest.mark.parametrize("text", NEUTRAL)
def test_a_plain_statement_is_left_alone(text):
    """Only questions/terse follow-ups are held; a declarative is not the gate's job."""
    assert relevance.passive(text) is False


def test_empty_is_not_passive_so_it_never_blocks_on_nothing():
    assert relevance.passive("") is False
    assert relevance.passive("   ") is False


# --- the gate: guard_spawn --------------------------------------------------

def _bind(text: str):
    """Run in a fresh context so the trigger contextvar can't leak between cases."""
    ctx = contextvars.copy_context()
    ctx.run(relevance.bind_trigger, text)
    return ctx


def test_disabled_is_a_pure_noop(monkeypatch):
    monkeypatch.delenv("ASTA_RELEVANCE", raising=False)
    ctx = _bind("No recent one..?")
    assert ctx.run(relevance.guard_spawn, "analysis", "look at repo") is None


def test_enabled_holds_a_passive_question_from_spawning_work(monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    ctx = _bind("No recent one..?")
    held = ctx.run(relevance.guard_spawn, "analysis", "analyse contmark-agent-harness")
    assert held is not None
    assert "held off" in held and "yes, go ahead" in held.lower()
    # and it was counted, so drift becomes a number rather than an anecdote
    counts = {r["outcome"]: r["n"] for r in store.outcome_counts(0.0) if r["kind"] == "relevance"}
    assert counts == {"held": 1}


def test_enabled_lets_a_real_command_through(monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    ctx = _bind("analyse the contmark-agent-harness repo")
    assert ctx.run(relevance.guard_spawn, "analysis", "analyse repo") is None
    assert [r for r in store.outcome_counts(0.0) if r["kind"] == "relevance"] == []


def test_a_followup_yes_go_ahead_passes_the_gate(monkeypatch):
    """The confirm the gate emits is answerable: his 'yes, go ahead' next turn is a
    command, so the second attempt spawns — no dead end."""
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    ctx = _bind("yes, go ahead")
    assert ctx.run(relevance.guard_spawn, "analysis", "analyse repo") is None


def test_teams_draft_is_not_guarded_it_is_already_approval_gated(monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    ctx = _bind("No recent one..?")
    assert ctx.run(relevance.guard_spawn, "teams_draft", "draft a reply") is None


def test_no_trigger_means_autonomous_spawn_never_blocked(monkeypatch):
    """A spawn with no user message behind it (the conductor loop, an accepted offer)
    has no question to have drifted from — leave it alone."""
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    ctx = _bind("")                            # nothing was said
    assert ctx.run(relevance.guard_spawn, "code", "do the thing") is None


# --- anchor drift: work aimed at a workspace the ask never named (measure-only) --

def _relevance_counts() -> dict:
    return {r["outcome"]: r["n"] for r in store.outcome_counts(0.0) if r["kind"] == "relevance"}


def _spawn_in_context(cid: str, trigger: str, kind: str, title: str, workspace: str):
    """guard_spawn with both the conversation and the trigger bound, as _run_turn does."""
    ctx = contextvars.copy_context()
    ctx.run(tasks.bind_conversation, cid)
    ctx.run(relevance.bind_trigger, trigger)
    return ctx.run(relevance.guard_spawn, kind, title, workspace)


def test_inherited_workspace_stamp_roundtrips():
    relevance.mark_inherited_workspace("c1", "contmark-agent-harness")
    assert relevance.inherited_workspace("c1") == "contmark-agent-harness"
    relevance.clear_inherited_workspace("c1")
    assert relevance.inherited_workspace("c1") == ""


def test_a_command_into_an_inherited_unnamed_workspace_is_drift(monkeypatch):
    """Even a genuine request for work is flagged when it lands in a repo the chat
    inherited silently and the ask never mentioned — the wrong-TARGET half."""
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    relevance.mark_inherited_workspace("c1", "contmark-agent-harness")
    held = _spawn_in_context("c1", "fix the retry timeout", "code",
                             "fix retry", "contmark-agent-harness")
    assert held is None                        # a command is never BLOCKED — only measured
    assert _relevance_counts() == {"drift": 1}


def test_naming_the_workspace_is_not_drift(monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    relevance.mark_inherited_workspace("c1", "contmark-agent-harness")
    _spawn_in_context("c1", "fix the retry in contmark", "code",
                      "fix retry", "contmark-agent-harness")
    assert _relevance_counts() == {}           # the ask referred to it — no drift


def test_spawning_into_a_different_workspace_is_not_drift(monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    relevance.mark_inherited_workspace("c1", "contmark-agent-harness")
    _spawn_in_context("c1", "fix the retry timeout", "code", "fix retry", "asta")
    assert _relevance_counts() == {}           # not the inherited anchor — nothing to flag


# --- semantic tier: did the ANSWER address the question? (measure-only) ----------

def test_offtopic_answer_to_a_question_is_recorded(monkeypatch):
    """Low word-overlap spends one local yes/no; a 'no' is recorded as off-topic —
    the quiet drift the intent gate can't see (no task was ever spawned)."""
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "no")
    import asyncio
    asyncio.run(relevance.judge_answer("Any message from Vinish?",
                                       "Repo hygiene of contmark looks fine."))
    assert _relevance_counts() == {"offtopic": 1}


def test_high_overlap_answer_is_free_and_unrecorded(monkeypatch):
    """The common case: the answer echoes the question's terms, so it's judged
    on-topic for zero tokens — the local model is never called, nothing is logged."""
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    called: list = []
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: called.append(1) or "no")
    import asyncio
    asyncio.run(relevance.judge_answer("Any message from Vinish?",
                                       "Nothing new from Vinish since his PR review."))
    assert called == []                        # pre-filter settled it — no model spend
    assert _relevance_counts() == {}


def test_local_model_down_skips_rather_than_guessing(monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)
    import asyncio
    asyncio.run(relevance.judge_answer("Any message from Vinish?", "Totally unrelated text."))
    assert _relevance_counts() == {}           # no oracle → no verdict, like the verify gate


def test_judge_only_runs_on_questions(monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "no")
    import asyncio
    asyncio.run(relevance.judge_answer("fix the login bug", "I changed the CSS."))
    assert _relevance_counts() == {}           # a command's reply is not judged for relevance


def test_judge_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("ASTA_RELEVANCE", raising=False)
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "no")
    import asyncio
    asyncio.run(relevance.judge_answer("Any message from Vinish?", "Unrelated."))
    assert _relevance_counts() == {}
