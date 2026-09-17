"""Hands, layer three — the screen and the mouse (Astra-class P7).

The fallback, and built so that being wrong is loud: elements by name, an
expectation checked after every step, and a refusal when a real door exists.
"""

from __future__ import annotations

import asyncio

import pytest

from app import apps, screen
from app.screen import Step


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ASTA_SCREEN", "1")
    monkeypatch.setenv("ASTA_APPS", "1")
    yield


def _screen_says(monkeypatch, *frames: str):
    """Stand in for the accessibility tree: each look() returns the next frame."""
    seen = list(frames)
    acts: list = []

    async def run(script, args):
        if "entire contents" in script:              # a look
            return seen.pop(0) if len(seen) > 1 else seen[0]
        acts.append({"script": script, "args": list(args)})
        return ""

    monkeypatch.setattr(apps, "_osascript", run)
    return acts


def test_a_click_that_changes_nothing_stops_the_sequence(monkeypatch):
    """The whole point. A click whose expectation does not come true is a FAILED
    step, not a step that happened to do nothing — otherwise the next step types
    into whatever now has focus."""
    acts = _screen_says(monkeypatch, "window: TextEdit\nbutton: New Document\n")
    steps = [Step("click", "New Document", expect="exists: Untitled"),
             Step("type", "hello", expect="exists: hello")]
    with pytest.raises(screen.ScreenError, match="did not come true"):
        asyncio.run(screen.follow("TextEdit", steps))
    assert len(acts) == 1, "it must not have carried on to the typing"


def test_each_step_is_checked_and_the_path_can_be_kept(monkeypatch):
    acts = _screen_says(monkeypatch,
                        "window: TextEdit\ntext: Untitled\n",
                        "window: TextEdit\ntext: Untitled\ntext: hello\n")
    steps = [Step("click", "New Document", expect="exists: Untitled"),
             Step("type", "hello", expect="exists: hello")]
    out = asyncio.run(screen.follow("TextEdit", steps, why="no scripting door"))
    assert out["ok"] and len(out["steps"]) == 2 and len(acts) == 2
    screen.remember_path("new note", "TextEdit", steps, why="no scripting door")
    assert "new note" in screen.paths()
    assert "TextEdit" in screen.describe_paths()


def test_the_mouse_is_refused_where_a_real_door_exists(monkeypatch):
    """Reminders answers AppleScript. Clicking at it trades a verified act for a
    fragile one — and the fragility shows up later, in front of someone."""
    _screen_says(monkeypatch, "window: Reminders\n")
    with pytest.raises(screen.ScreenError, match="has a proper door"):
        asyncio.run(screen.follow("Reminders",
                                  [Step("click", "New", expect="exists: New")]))


def test_a_sequence_that_checks_nothing_at_the_end_is_refused(monkeypatch):
    _screen_says(monkeypatch, "window: X\n")
    with pytest.raises(screen.ScreenError, match="nothing to check"):
        asyncio.run(screen.follow("TextEdit", [Step("click", "Save")]))


def test_gone_is_an_expectation_too(monkeypatch):
    _screen_says(monkeypatch, "window: TextEdit\n")
    out = asyncio.run(screen.follow(
        "TextEdit", [Step("click", "Save", expect="gone: button: Save")]))
    assert out["ok"]


def test_it_is_off_unless_he_turns_it_on(monkeypatch):
    monkeypatch.delenv("ASTA_SCREEN", raising=False)
    with pytest.raises(screen.ScreenError, match="screen fallback is off"):
        asyncio.run(screen.follow("TextEdit", [Step("click", "x", expect="exists: y")]))


def test_a_missing_app_is_said_plainly(monkeypatch):
    async def run(script, args):
        return "NO-PROCESS"
    monkeypatch.setattr(apps, "_osascript", run)
    with pytest.raises(screen.ScreenError, match="is not running"):
        asyncio.run(screen.look("TextEdit"))


def test_a_saved_path_replays_and_an_unknown_one_says_what_exists(monkeypatch):
    _screen_says(monkeypatch,
                 "window: TextEdit\ntext: hello\n",
                 "window: TextEdit\ntext: hello\n")
    screen.remember_path("say hello", "TextEdit",
                         [Step("type", "hello", expect="exists: hello")])
    assert asyncio.run(screen.replay("say hello"))["ok"]
    with pytest.raises(screen.ScreenError, match="no saved path"):
        asyncio.run(screen.replay("nonsense"))


def test_a_permission_failure_is_an_answer_not_a_crash(monkeypatch):
    """Live on 17 Sep: the first real screen call raised AppError out of
    use_screen and the server returned a 500 — a brain would have been handed an
    exception instead of a sentence it can pass on."""
    from app import agent

    async def refuse(script, args):
        raise apps.AppError("not allowed")

    monkeypatch.setattr(apps, "_osascript", refuse)
    out = asyncio.run(agent.use_screen(
        "TextEdit", [{"do": "menu", "target": "File > New", "expect": "exists: Untitled"}]))
    assert out.startswith("Not done")


def test_the_grant_it_names_is_the_one_that_is_actually_missing(monkeypatch):
    """-1719 is ACCESSIBILITY ("not allowed assistive access"); -1743 is
    AUTOMATION. Both used to be reported as Automation, which sends him to the
    wrong page of System Settings and leaves the real switch off."""
    import types

    def fake_proc(stderr: bytes):
        class P:
            returncode = 1
            async def communicate(self, _input=None):
                return b"", stderr
            def kill(self):
                pass
        return P()

    async def exec_1719(*a, **k):
        return fake_proc(b"execution error: System Events got an error: "
                         b"osascript is not allowed assistive access. (-1719)")

    async def exec_1743(*a, **k):
        return fake_proc(b"execution error: Not authorised to send Apple events "
                         b"to Reminders. (-1743)")

    monkeypatch.setattr(apps.shutil, "which", lambda n: "/usr/bin/osascript")
    monkeypatch.setattr(apps.asyncio, "create_subprocess_exec", exec_1719)
    with pytest.raises(apps.AppError, match="Accessibility"):
        asyncio.run(apps._osascript("x", []))
    monkeypatch.setattr(apps.asyncio, "create_subprocess_exec", exec_1743)
    with pytest.raises(apps.AppError, match="Automation"):
        asyncio.run(apps._osascript("x", []))
