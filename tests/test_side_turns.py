"""A read-only question must not wait behind a forty-minute implementation.

From a real WhatsApp transcript, 2026-08-26: Arun asked "What is the ci status of
above PR" while a code task was running and got "still finishing the previous one
— I'll answer this right after." Reading a PR's checks does not conflict with
writing code; the serialisation was protecting the conversation, not the repo.
"""

from __future__ import annotations

import asyncio

import pytest

from app import activity, capabilities


def test_a_read_only_question_is_independent():
    for text in ("What is the ci status of above PR",
                 "Does it passed",
                 "what did vinish say",
                 "which repo is that in?",
                 "how many tests are failing?"):
        assert activity.classify_interjection(text) == "independent", text


def test_anything_that_writes_is_never_independent():
    """The classifier must not hand a request to the concurrent path.

    Question-shaped is not the same as read-only: "can you push this?" is a
    request. These stay ambiguous and are queued, which is the old behaviour.
    """
    for text in ("can you push this to main?",
                 "implement the retry logic",
                 "could you merge that PR?",
                 "should I send this to Vinish? send it",
                 "delete that branch"):
        assert activity.classify_interjection(text) != "independent", text


def test_augment_and_redirect_are_untouched():
    """`independent` may only ever narrow what would have been `ambiguous`."""
    assert activity.classify_interjection("also add tests for that") == "augment"
    assert activity.classify_interjection("no stop, do the other thing") == "redirect"


def test_an_empty_or_huge_message_is_not_independent():
    assert not activity.is_read_only_ask("")
    assert not activity.is_read_only_ask("what " * 200)


def test_a_side_turn_cannot_reach_a_capability_that_writes():
    """The guarantee that makes the classifier safe to be imperfect.

    Safety comes from the toolset, not from getting the intent right. The worst a
    misclassified message can do is read something and answer it.
    """
    token = capabilities.READ_ONLY_TURN.set(True)
    try:
        names = {fn.__name__ for fn in capabilities.tools_for(None)}
    finally:
        capabilities.READ_ONLY_TURN.reset(token)

    writers = [n for n, c in capabilities.registry().items() if c.write]
    assert writers, "no write capabilities declared — the test proves nothing"
    leaked = sorted(set(writers) & names)
    assert not leaked, f"a side turn could reach write capabilities: {leaked}"
    assert "teams_call" not in names
    assert "resolve_context" in names, "it must still be able to answer questions"


def test_a_normal_turn_keeps_every_capability():
    """The restriction must apply to side turns only."""
    token = capabilities.READ_ONLY_TURN.set(False)
    try:
        names = {fn.__name__ for fn in capabilities.tools_for(None)}
    finally:
        capabilities.READ_ONLY_TURN.reset(token)
    assert "teams_call" in names


def test_the_flag_does_not_leak_into_the_turn_already_running():
    """asyncio copies context at task creation, which is the whole mechanism.

    The side turn must be read-only and the turn that was already running must
    not be — if the flag leaked, an implementation in flight would abruptly lose
    its ability to write.
    """
    seen: dict[str, bool] = {}

    async def already_running():
        await asyncio.sleep(0.05)
        seen["primary"] = capabilities.READ_ONLY_TURN.get()

    async def side():
        seen["side"] = capabilities.READ_ONLY_TURN.get()

    async def main():
        primary = asyncio.create_task(already_running())
        token = capabilities.READ_ONLY_TURN.set(True)
        try:
            helper = asyncio.create_task(side())
        finally:
            capabilities.READ_ONLY_TURN.reset(token)
        await asyncio.gather(primary, helper)
        seen["caller"] = capabilities.READ_ONLY_TURN.get()

    asyncio.run(main())
    assert seen["side"] is True, "the side turn was not read-only"
    assert seen["primary"] is False, "the running turn lost its write tools"
    assert seen["caller"] is False, "the flag leaked back to the dispatcher"


def test_side_turns_are_bounded():
    """Each one is a real brain turn; an unbounded fan-out is a billing burst."""
    from app import main
    assert main.SIDE_TURNS_MAX >= 1
    assert main.SIDE_TURNS_MAX <= 5, "an unbounded-in-practice concurrency limit"
