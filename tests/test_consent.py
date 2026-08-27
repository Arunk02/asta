"""The 27 August substitution: asked for a call, given a code push.

Arun asked, in these words: "Call Vinish and discuss on the 1409 PR he gave some
comments in the chat and try to resolve with him."

What happened instead, in order:

  1. `tool_index` ranked the call tools out of the top-8 for that message.
  2. The model cannot see that a selection happened, so it reported the gap as a
     fact about Asta — "I can't hold a live conversation with Vinish myself".
  3. `delegate_task` is in the ALWAYS floor, so it was reachable anyway.
  4. It spawned task #72 to rewrite seven review findings and push them.
  5. Told to stop, it said "no cancel/stop tool is available to me … it will push
     when done unless you intervene directly". `tasks.cancel` exists and kills the
     worker; `reject_task` simply had not been selected either.

Every step was individually defensible and the sum was: the act he asked for did
not happen, an irreversible one he never asked for did, and he was named as the
only way to stop it — on the day he said "i cant everytime come and fix".

These tests pin each link. They are written against his real sentences rather than
invented ones, because the classifier only has to work on how he actually types.
"""

from __future__ import annotations

import pytest

from app import capabilities, consent, tool_index

#: His message, verbatim.
ASKED_TO_CALL = ("Call Vinish and discuss on the 1409 PR he gave some comments "
                 "in the chat and try to resolve with him")
#: And the follow-up, once the code task was already running.
ASKED_TO_DISCUSS = "Dont push change blindly discuss with them , does it really matter"


# --- rule one: asking is the consent -----------------------------------------

def test_his_own_words_count_as_asking_for_a_call():
    assert consent.asked_to_call(ASKED_TO_CALL)


def test_asking_to_discuss_is_asking_for_a_person_not_a_call():
    """He wanted them engaged; he did not say by phone. Rule two still applies."""
    assert consent.asked_to_talk(ASKED_TO_DISCUSS)
    assert not consent.asked_to_call(ASKED_TO_DISCUSS)


@pytest.mark.parametrize("text", [
    "call vinish",
    "can you call vinish",
    "ring vinish",
    "give vinish a call",
    "call him back",
    "get him on the phone",
])
def test_the_ways_he_asks_for_a_call(text):
    assert consent.asked_to_call(text), text


@pytest.mark.parametrize("text", [
    "what does this function call do",
    "the api call fails intermittently",
    "summarise the call notes",
    "I will call you later",
    "should i call you",
])
def test_the_word_call_is_not_always_a_request_to_ring_somebody(text):
    """A false positive here dials a colleague because he mentioned a stack trace.
    That is worse than missing one, so the noun senses must all be excluded."""
    assert not consent.asked_to_call(text), text


@pytest.mark.parametrize("text", [
    "dont call him, just fix it",
    "no need to call vinish",
    "fix it instead of calling him",
])
def test_a_negated_call_is_not_a_request(text):
    assert not consent.asked_to_call(text), text


def test_negation_scopes_to_the_act_it_precedes():
    """"Dont push change blindly discuss with them" negates the push, not the
    discussion — the whole point of the sentence is the thing he DOES want."""
    assert consent.asked_to_talk(ASKED_TO_DISCUSS)


# --- rule two: no substitution ------------------------------------------------

def test_a_code_task_may_not_replace_the_call_he_asked_for():
    reason = consent.substitution(ASKED_TO_CALL, "code")
    assert reason, "the 27 August spawn would still be allowed"
    assert "not to change code" in reason


def test_the_refusal_tells_him_how_to_get_what_he_wanted():
    """A block that only says no leaves him exactly where the substitution did:
    without his call. It has to name the way forward."""
    reason = consent.substitution(ASKED_TO_CALL, "code")
    assert "use the call tools" in reason
    assert "alongside" in reason          # and how to get the code work too


def test_asking_for_both_a_call_and_the_work_allows_the_spawn():
    """Refusing a task he DID ask for is its own failure — narrower is safer here."""
    assert not consent.substitution("call vinish and fix the eta validation", "code")


def test_plain_code_work_is_untouched():
    assert not consent.substitution("fix the ETA validation in booking", "code")
    assert not consent.substitution("implement the retry logic", "code")


def test_analysis_tasks_are_not_blocked():
    """An analysis spawn reads and reports. Getting it wrong costs a paragraph, not
    a branch, so it is not worth a gate that can misfire."""
    assert not consent.substitution(ASKED_TO_CALL, "analysis")


def test_no_turn_text_blocks_nothing():
    """Background paths set no TURN_TEXT. Absence must not be read as a violation,
    or every scheduled task stops."""
    assert not consent.substitution("", "code")


# --- the floor: what can start must be stoppable ------------------------------

def test_the_tool_that_stops_a_task_is_always_reachable():
    """`delegate_task` is in ALWAYS, so irreversible work can start on any turn.
    Its antidote has to be there too, or the floor guarantees a start that cannot
    be taken back — which is exactly what he was told."""
    assert "delegate_task" in capabilities.ALWAYS
    assert "reject_task" in capabilities.ALWAYS


def test_reject_task_is_a_real_capability_not_just_a_name():
    assert "reject_task" in capabilities.registry()


# --- routing: the act he named always gets its tools --------------------------

def test_asking_for_a_call_always_selects_the_call_tools():
    """The ranker may be wrong about what a message is like. It must not be wrong
    about what the message literally says to do."""
    required = tool_index.required_for(ASKED_TO_CALL)
    assert "discuss_in_call" in required
    assert "teams_call" in required


def test_the_selection_actually_carries_them(monkeypatch):
    """required_for is only worth having if select() honours it. Forces a ranking
    that omits the call tools and asserts they survive anyway."""
    monkeypatch.setattr(tool_index, "enabled", lambda: True)
    monkeypatch.setattr(tool_index, "rank",
                        lambda q: [("ci_status", 0.9), ("health_check", 0.8)])
    chosen = tool_index.select(ASKED_TO_CALL)
    assert chosen is not None
    assert "discuss_in_call" in chosen
    assert "reject_task" in chosen


def test_an_unrelated_message_pulls_in_no_call_tools():
    """The guarantee must not become "always expose everything", which would undo
    the whole point of narrowing."""
    assert tool_index.required_for("what is the ci status") == []
    assert tool_index.required_for("summarise my unread mail") == []


# --- the capability exists at all ---------------------------------------------

def test_asta_can_hold_a_conversation():
    """The refusal was "I can't hold a live conversation with Vinish myself". Every
    part of that loop was proven live the same day and left in a scratch file."""
    assert "discuss_in_call" in capabilities.registry()


def test_conversation_is_a_write_and_says_so():
    cap = capabilities.registry()["discuss_in_call"]
    assert cap.write
    assert "cannot hold a live conversation" in cap.note   # the sentence never to say


def test_the_call_note_no_longer_claims_it_only_stages():
    """The note taught every brain that a call "STAGES, does not dial" — which is
    why asking for one produced a question back instead of a ringing phone."""
    note = capabilities.registry()["teams_call"].note
    assert "STAGES, does not dial" not in note
    assert "DIALS" in note


# --- the tools themselves, not just the policy --------------------------------
# The classifier being right is worth nothing if `teams_call` still stages, so
# these drive the actual tool functions with the real contextvar set.

import asyncio                                                    # noqa: E402

from app import agent as agent_mod                                # noqa: E402


@pytest.fixture
def _teams_on(monkeypatch):
    from app import teams_bridge
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)


@pytest.fixture
def _turn(monkeypatch):
    """Set what Arun typed this turn, the way main.py does."""
    def _set(text: str):
        capabilities.TURN_TEXT.set(text)
    yield _set
    capabilities.TURN_TEXT.set("")


def test_a_call_he_asked_for_rings_instead_of_asking_him_again(monkeypatch, _teams_on, _turn):
    """His question, in code: "if i ask to call, then im aware right still what is
    the issue?" Nothing — so it dials."""
    from app import offers, ops
    dialled = {}

    async def _fake_run(spec):
        dialled.update(spec)
        return "📞 Calling Vinish Kumar"

    monkeypatch.setattr(ops, "run", _fake_run)
    monkeypatch.setattr(offers, "staged_write",
                        lambda *a, **k: pytest.fail("staged a call he asked for"))
    _turn(ASKED_TO_CALL)
    out = asyncio.run(agent_mod.teams_call("Vinish"))
    assert dialled["name"] == "teams_call"
    assert dialled["args"] == {"who": "Vinish", "video": False}
    assert "Calling" in out


def test_a_call_asta_thought_of_itself_still_waits_for_his_yes(monkeypatch, _teams_on, _turn):
    """The gate is not removed, it is moved to where it belongs: an act he did not
    ask for, whose first sign would be a colleague's phone ringing."""
    from app import offers, ops
    staged = {}
    monkeypatch.setattr(offers, "staged_write",
                        lambda op, args, *a, **k: staged.update({"op": op, "args": args}))
    monkeypatch.setattr(ops, "run",
                        lambda spec: pytest.fail("dialled without being asked"))
    _turn("what did vinish say about the PR")
    out = asyncio.run(agent_mod.teams_call("Vinish"))
    assert staged["op"] == "teams_call"
    assert "waiting for Arun's yes" in out
    assert "Nothing is ringing yet" in out


def test_a_failed_call_says_which_part_failed(monkeypatch, _teams_on, _turn):
    """Never "I can't call". The failure has a cause and he needs it."""
    from app import ops

    async def _boom(spec):
        raise RuntimeError("no person match for 'Vinish' (saw: nothing)")

    monkeypatch.setattr(ops, "run", _boom)
    _turn(ASKED_TO_CALL)
    out = asyncio.run(agent_mod.teams_call("Vinish"))
    assert "no person match" in out
    assert "Nothing rang" in out


def test_discuss_returns_at_once_and_talks_in_the_background(monkeypatch, _teams_on):
    """A call lasts minutes; a chat turn must not. The reply comes back immediately
    and the conversation reports itself when it ends."""
    from app import conversation
    ran = asyncio.Event()

    async def _fake_converse(who, topic, workspace=""):
        ran.set()
        return f"Talked to {who} about {topic}"

    monkeypatch.setattr(conversation, "converse", _fake_converse)

    async def _drive():
        out = await agent_mod.discuss_in_call("Vinish", "the 1409 review comments")
        await asyncio.wait_for(ran.wait(), timeout=2)
        return out

    out = asyncio.run(_drive())
    assert "Calling Vinish" in out
    assert "won't commit you to anything" in out


def test_a_code_task_is_refused_when_he_asked_for_the_call(monkeypatch, _turn):
    """End to end through the real tool: the exact spawn that produced task #72."""
    from app import tasks
    monkeypatch.setattr(tasks, "spawn",
                        lambda *a, **k: pytest.fail("spawned instead of calling"))
    _turn(ASKED_TO_CALL)
    out = agent_mod.delegate_task("Fix PR 1409 review findings",
                                  "Fix all 7 items and push", kind="code",
                                  workspace="booking")
    assert "not to change code" in out


def test_the_same_spawn_is_fine_when_he_asked_for_it(monkeypatch, _turn):
    from app import relevance, tasks
    monkeypatch.setattr(relevance, "guard_spawn", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "refinable_match", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "spawn", lambda *a, **k: {"id": 99})
    _turn("fix the 7 review findings on PR 1409")
    out = agent_mod.delegate_task("Fix PR 1409", "…", kind="code", workspace="booking")
    assert "#99" in out


# --- never report a narrowed toolset as a missing capability ------------------

def test_a_narrowed_turn_is_told_it_is_narrowed():
    text = agent_mod.build_instructions("", "", None, "web", ["ci_status"])
    assert "narrowed by retrieval" in text
    assert "never tell Arun that Asta *cannot* do something" in text


def test_a_full_toolset_carries_no_such_note():
    """It would be false, and it costs tokens on every unnarrowed turn."""
    assert "narrowed by retrieval" not in agent_mod.build_instructions("", "", None, "web", None)


def test_the_note_names_both_things_it_wrongly_denied():
    """Live calls and cancelling a task — the two false denials of 27 August."""
    assert "two-way conversation" in agent_mod.NARROWED
    assert "cancelling" in agent_mod.NARROWED
