"""Opening what he names — Round 3, P11.

"i ask to open intelij, open chrome, open youtube it has to open". There were
seven named recipes for putting things INTO apps and no way to open one.

The machine is stubbed here; what is tested is the deciding — which app he
meant, whether the sentence is a launch at all, and the part that matters most:
an app that did not actually start is reported as not started.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import apps, policy


@pytest.fixture(autouse=True)
def _mac(monkeypatch):
    """A fixed machine: his laptop's /Applications must not decide a test."""
    installed = [Path(f"/Applications/{n}.app") for n in
                 ("IntelliJ IDEA", "IntelliJ IDEA CE", "Google Chrome", "Safari",
                  "Microsoft Teams", "Microsoft Outlook", "LM Studio", "Reminders")]
    monkeypatch.setattr(apps, "installed_apps", lambda refresh=False: list(installed))
    monkeypatch.setattr(apps, "bundle_id", lambda app: f"test.{Path(app).stem.lower()}")
    monkeypatch.setattr(apps, "enabled", lambda: True)
    monkeypatch.setattr(apps, "START_WAIT", 0.3)
    return installed


@pytest.fixture
def machine(monkeypatch):
    """Records what was opened, and what is running afterwards."""
    state = {"ran": [], "running": set()}

    async def run(*argv):
        state["ran"].append([str(a) for a in argv])
        for a in argv[1:]:
            if str(a).endswith(".app"):
                state["running"].add(f"test.{Path(a).stem.lower()}")
        if not any(str(a).endswith(".app") for a in argv[1:]):
            state["running"].add("com.google.chrome")
        return 0, ""

    async def running():
        return set(state["running"])

    monkeypatch.setattr(apps, "_run", run)
    monkeypatch.setattr(apps, "running_bundles", running)
    return state


# --- which one did he mean? ------------------------------------------------------

@pytest.mark.parametrize("said, want", [
    ("intellij", "IntelliJ IDEA"),        # not the CE beside it: the shorter name
    ("IntelliJ IDEA CE", "IntelliJ IDEA CE"),
    ("chrome", "Google Chrome"),
    ("teams", "Microsoft Teams"),
    ("teams microsoft", "Microsoft Teams"),   # his words, in his order
    ("lm studio", "LM Studio"),
])
def test_he_names_it_loosely_and_the_right_app_is_found(said, want):
    app, _ = apps.find_app(said)
    assert app is not None and app.stem == want


def test_an_app_he_does_not_have_is_not_invented():
    app, near = apps.find_app("photoshop")
    assert app is None and "IntelliJ IDEA" not in near


def test_the_other_candidates_come_back_so_the_answer_can_say_so():
    _, others = apps.find_app("intellij")
    assert others == ["IntelliJ IDEA CE"]


# --- is this sentence a launch at all? -------------------------------------------

@pytest.mark.parametrize("said, want", [
    ("open intellij", ("app", "intellij", "")),
    ("fire up teams", ("app", "teams", "")),
    ("can you open outlook please", ("app", "outlook", "")),
    ("open youtube", ("url", "youtube", "")),
    ("open youtube in chrome", ("url", "youtube", "chrome")),
    ("open docs.python.org", ("url", "docs.python.org", "")),
    # not launches
    ("open my reminders", None),          # about the list, not the window
    ("open the PR for 1440", None),
    ("what is open?", None),
    ("open photoshop", None),             # not installed: a brain can say something useful
    ("can you open the door for a new booking and clone it", None),
])
def test_only_a_launch_takes_the_fast_path(said, want):
    assert apps.open_ask(said) == want


# --- and did it actually open? ---------------------------------------------------

def test_opening_an_app_says_so_only_after_seeing_it_running(machine):
    out = asyncio.run(apps.open_app("intellij"))
    assert out["ok"] and out["app"] == "IntelliJ IDEA"
    assert machine["ran"][0][:2] == [apps.OPEN, "-a"]
    assert "IntelliJ IDEA.app" in machine["ran"][0][2]


def test_an_app_that_never_starts_is_reported_as_not_started(monkeypatch, machine):
    """`open` exits 0 having done nothing all the time. The claim is "it is in
    front of you", and that is checked."""
    async def nothing_starts():
        return set()

    monkeypatch.setattr(apps, "running_bundles", nothing_starts)
    out = asyncio.run(apps.open_app("intellij"))
    assert out["verified"] is False and "not running" in out["said"]
    assert "⚠️" in asyncio.run(apps.open_it("app", "intellij"))


def test_not_being_allowed_to_look_is_not_the_same_as_it_failing(monkeypatch, machine):
    """Until macOS allows Asta to ask System Events, the check cannot run. That
    is "I can't tell", and calling it a failure would report every successful
    launch as a failed one."""
    async def cannot_see():
        return None

    monkeypatch.setattr(apps, "running_bundles", cannot_see)
    out = asyncio.run(apps.open_app("intellij"))
    assert out["verified"] is None and "can't confirm" in out["said"]
    assert "Automation" in out["said"]
    assert asyncio.run(apps.open_it("app", "intellij")).startswith("🖥")


def test_running_is_read_from_system_events_not_by_asking_the_app(monkeypatch):
    """Asking an app whether it is running LAUNCHES it — the check would then
    be its own answer."""
    seen = {}

    async def osascript(script, args):
        seen["script"] = script
        return "com.apple.safari, com.google.chrome"

    monkeypatch.setattr(apps, "_osascript", osascript)
    assert "com.google.chrome" in (asyncio.run(apps.running_bundles()) or set())
    assert "System Events" in seen["script"] and "is running" not in seen["script"]


def test_a_site_opens_in_the_browser_he_named(machine):
    out = asyncio.run(apps.open_url("youtube", "chrome"))
    assert out["ok"] and out["url"] == "https://www.youtube.com"
    assert machine["ran"][0][1] == "-a" and "Google Chrome.app" in machine["ran"][0][2]


def test_a_site_with_no_browser_named_goes_to_his_default(machine):
    out = asyncio.run(apps.open_url("docs.python.org/3"))
    assert out["ok"] and out["url"] == "https://docs.python.org/3"
    assert machine["ran"][0] == [apps.OPEN, "https://docs.python.org/3"]


def test_nonsense_is_not_opened_as_a_site(machine):
    with pytest.raises(apps.AppError):
        asyncio.run(apps.open_url("some thing he said"))
    assert machine["ran"] == []


def test_a_standing_rule_stops_a_launch(machine):
    policy.add("never", "app", "IntelliJ IDEA", words="never open intellij")
    out = asyncio.run(apps.open_it("app", "intellij"))
    assert "⚠️" in out and "standing rule" in out
    assert machine["ran"] == []


def test_with_the_doors_off_nothing_opens(monkeypatch, machine):
    monkeypatch.setattr(apps, "enabled", lambda: False)
    assert "ASTA_APPS=1" in asyncio.run(apps.open_it("app", "intellij"))
    assert machine["ran"] == []
