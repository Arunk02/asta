"""Calling by name, after a real call to a real colleague exposed the race.

What happened: headless `resolve` finds "Vinish Kumar" every time. The HEADED
window opened a chat titled 'Author' and aborted — correctly, because dialling a
chat whose title does not match would ring a stranger on Arun's behalf. But
aborting on the FIRST mismatch made calling by name fail every time rather than
merely be slow, so the safety check turned a race into a permanent outage of the
feature.

The two properties that have to hold together, and which pull in opposite
directions: it must retry a mismatch (it is a timing artefact of a slow-painting
headed window), and it must still never dial the wrong person.
"""

from __future__ import annotations

import pytest

from app import meetings


class _Page:
    """Just enough page for the retry loop: Escape is all it touches."""

    def __init__(self):
        self.escapes = 0
        self.keyboard = self

    async def press(self, key):
        if key == "Escape":
            self.escapes += 1


@pytest.mark.asyncio
async def test_a_mismatch_is_retried_and_succeeds_once_the_rail_settles(monkeypatch):
    calls = {"n": 0}

    async def flaky(page, who, allow_group=False):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"opened 'Author' instead of '{who}' — aborted without typing")
        return "Vinish Kumar"

    monkeypatch.setattr("app.teams_bridge._find_chat", flaky)
    monkeypatch.setattr(meetings, "_FIND_BACKOFF", 0.0)
    page = _Page()
    assert await meetings._find_chat_settled(page, "vinish") == "Vinish Kumar"
    assert calls["n"] == 3
    # A half-open search box is what makes the next attempt land on the same wrong
    # row, so each retry clears it.
    assert page.escapes == 2


@pytest.mark.asyncio
async def test_it_still_refuses_rather_than_dialling_the_wrong_person(monkeypatch):
    async def always_wrong(page, who, allow_group=False):
        raise RuntimeError(f"opened 'Author' instead of '{who}' — aborted without typing")

    monkeypatch.setattr("app.teams_bridge._find_chat", always_wrong)
    monkeypatch.setattr(meetings, "_FIND_BACKOFF", 0.0)
    with pytest.raises(RuntimeError) as exc:
        await meetings._find_chat_settled(_Page(), "vinish")
    assert "nothing was dialled" in str(exc.value)
    assert "Author" in str(exc.value), "the message must still name what it opened"


@pytest.mark.asyncio
async def test_a_group_refusal_is_not_retried_into_succeeding(monkeypatch):
    """Retrying a timing problem is right. Retrying a POLICY refusal is not —
    'call the group' dials every member at once, and trying again four times does
    not make that any more what he meant."""
    calls = {"n": 0}

    async def group(page, who, allow_group=False):
        calls["n"] += 1
        raise RuntimeError("that is a group chat — refusing to call a group")

    monkeypatch.setattr("app.teams_bridge._find_chat", group)
    monkeypatch.setattr(meetings, "_FIND_BACKOFF", 0.0)
    with pytest.raises(RuntimeError, match="group"):
        await meetings._find_chat_settled(_Page(), "team booking")
    assert calls["n"] == 1, "a policy refusal must be raised immediately"


@pytest.mark.asyncio
async def test_the_first_attempt_costs_nothing_when_it_works(monkeypatch):
    async def fine(page, who, allow_group=False):
        return "Vinish Kumar"

    monkeypatch.setattr("app.teams_bridge._find_chat", fine)
    page = _Page()
    assert await meetings._find_chat_settled(page, "vinish") == "Vinish Kumar"
    assert page.escapes == 0, "the happy path must not press keys at the UI"


def test_warming_is_wired_into_every_path_that_opens_its_mouth():
    """The 11.4-second cold start was paid in front of whoever picked up, because
    warm_the_voice() was called from join_by_phrase and nowhere else."""
    import inspect
    src = inspect.getsource(meetings)
    for fn in ("async def call_person", "async def join("):
        body = src.split(fn, 1)[1].split("\nasync def ", 1)[0]
        assert "warm_the_voice()" in body, f"{fn} does not warm the voice model"


def test_no_headed_launch_shares_the_profile_with_the_pool():
    """One writer per user-data-dir. A headed call that launches while the pooled
    headless browser is alive is the collision that cost fourteen hours."""
    import inspect
    src = inspect.getsource(meetings)
    for chunk in src.split("_launch(headless=False)")[:-1]:
        assert "close_pool()" in chunk.rsplit("async def ", 1)[-1], (
            "a headed launch runs without dropping the pool first")
