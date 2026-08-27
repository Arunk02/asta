"""The bench must never reach a person. This proves it, rather than promising it.

Arun's instruction was explicit — measuring Asta must not call anyone. Today the
runners are pure functions, so today it is true by accident. This is the test that
keeps it true after somebody adds a scenario for "how good is it at replying".
"""

from __future__ import annotations

import pytest

from app import sandbox


@pytest.mark.asyncio
async def test_sending_a_teams_message_is_blocked_and_never_reaches_the_real_one():
    from app import teams_bridge
    real = teams_bridge.send_message
    with sandbox.sealed() as tripped:
        assert teams_bridge.send_message is not real, "the seal did not take"
        with pytest.raises(sandbox.OutwardMoveBlocked):
            await teams_bridge.send_message("anyone", "hello")
    assert tripped == ["teams_bridge.send_message"]
    assert teams_bridge.send_message is real, "the seal must not leak"


@pytest.mark.asyncio
async def test_calling_a_person_is_blocked():
    from app import meetings
    with sandbox.sealed():
        with pytest.raises(sandbox.OutwardMoveBlocked):
            await meetings.call_person("anyone")


@pytest.mark.asyncio
async def test_no_browser_can_be_launched_at_all():
    """The choke point. No Chromium means no Teams tab, so no call can be
    dialled and no compose window submitted, whatever a runner believes it does."""
    from app import teams_bridge
    with sandbox.sealed():
        with pytest.raises(sandbox.OutwardMoveBlocked):
            await teams_bridge._launch()


@pytest.mark.asyncio
async def test_a_notification_cannot_be_pushed_from_a_bench_run():
    from app import notify
    with sandbox.sealed():
        with pytest.raises(sandbox.OutwardMoveBlocked):
            await notify.notify("this must not buzz his phone")


def test_the_seal_is_restored_even_when_the_body_raises():
    from app import telegram
    real = telegram.send
    with pytest.raises(ValueError):
        with sandbox.sealed():
            raise ValueError("the bench itself blew up")
    assert telegram.send is real, (
        "a seal that leaks on error leaves Asta unable to notify him about the "
        "very failure that broke the bench")


def test_the_guarded_surface_has_not_silently_shrunk():
    """A seal is only as good as its list. If someone deletes an entry to make a
    scenario pass, this is what notices."""
    covered = set(sandbox.covers())
    for essential in ("teams_bridge._launch", "teams_bridge.send_message",
                      "meetings.call_person", "meetings.join",
                      "notify.notify", "jira.add_comment"):
        assert essential in covered, f"{essential} is no longer sealed"


@pytest.mark.asyncio
async def test_the_bench_runs_inside_the_seal():
    """Not "the seal works" but "the bench uses it" — the gap those two leave is
    where an accident lives."""
    from app import bench, runners

    reached: list[str] = []

    async def nosy(case):
        from app import teams_bridge
        try:
            await teams_bridge.send_message("someone", "hi")
            reached.append("SENT")
        except sandbox.OutwardMoveBlocked:
            reached.append("blocked")
        return {"text": "action=True"}

    original = runners.RUNNERS.get("triage")
    runners.RUNNERS["triage"] = nosy
    try:
        await bench.run("triage")
    finally:
        if original is not None:
            runners.RUNNERS["triage"] = original
    assert reached and set(reached) == {"blocked"}, (
        f"a bench scenario reached the real sender: {reached}")
