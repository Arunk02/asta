"""Hands, layer two — his apps through their own doors (Astra-class P7).

A brain picks a named recipe and fills in arguments; it never writes a script,
and nothing counts as done until the app has been read back.
"""

from __future__ import annotations

import asyncio

import pytest

from app import apps


@pytest.fixture(autouse=True)
def _doors_on(monkeypatch):
    monkeypatch.setenv("ASTA_APPS", "1")
    yield


def _fake_runner(monkeypatch, *, wrote: list, reads: str = ""):
    """Stand in for osascript: record what it was asked, answer the read-back."""
    async def run(script, args):
        wrote.append({"script": script, "args": list(args)})
        return reads
    monkeypatch.setattr(apps, "_osascript", run)


def test_a_title_is_passed_as_an_argument_never_pasted_into_the_script(monkeypatch):
    """His titles have quotes and em dashes in them. A script built by string
    concatenation is the AppleScript spelling of SQL injection."""
    calls: list = []
    nasty = 'Ring "Alex" — don\'t forget; quit application "Calendar"'
    _fake_runner(monkeypatch, wrote=calls, reads=nasty)
    out = asyncio.run(apps.run("reminder_add", title=nasty, note=""))
    assert out["ok"] and out["verified"] == nasty
    for call in calls:
        assert nasty not in call["script"], "the title reached the script body"
        assert nasty in call["args"]


def test_a_write_that_the_app_cannot_show_afterwards_is_a_failure(monkeypatch):
    """osascript exits 0 having done nothing all the time. The act is what the
    app says is there afterwards, not what the interpreter returned."""
    calls: list = []
    _fake_runner(monkeypatch, wrote=calls, reads="something else entirely")
    with pytest.raises(apps.AppError, match="did not come back with"):
        asyncio.run(apps.run("reminder_add", title="Nudge the reviewer"))


def test_a_read_only_recipe_needs_no_read_back(monkeypatch):
    calls: list = []
    _fake_runner(monkeypatch, wrote=calls, reads="Buy milk\nNudge the reviewer")
    out = asyncio.run(apps.run("reminders_open"))
    assert out["ok"] and len(calls) == 1 and "verified" not in out


def test_a_missing_argument_is_refused_before_the_app_is_touched(monkeypatch):
    calls: list = []
    _fake_runner(monkeypatch, wrote=calls, reads="")
    with pytest.raises(apps.AppError, match="needs title"):
        asyncio.run(apps.run("reminder_add", note="no title given"))
    with pytest.raises(apps.AppError, match="no such recipe"):
        asyncio.run(apps.run("delete_everything", title="x"))
    assert calls == [], "nothing should have reached the app"


def test_the_doors_are_shut_unless_he_opens_them(monkeypatch):
    monkeypatch.delenv("ASTA_APPS", raising=False)
    calls: list = []
    _fake_runner(monkeypatch, wrote=calls, reads="")
    with pytest.raises(apps.AppError, match="app doors are off"):
        asyncio.run(apps.run("reminder_add", title="x"))
    assert calls == []


def test_a_rule_of_his_stops_a_door(monkeypatch):
    """The same policy gate as everything else — one rule, not a per-tool copy."""
    from app import policy
    policy.add("never", act="app", target="calendar",
               words="don't touch my calendar")
    calls: list = []
    _fake_runner(monkeypatch, wrote=calls, reads="Standup")
    with pytest.raises(apps.AppError, match="a rule of his"):
        asyncio.run(apps.run("calendar_event_add", title="Standup",
                             start="2026-09-17 09:30", minutes="15"))
    assert calls == []


def test_mail_can_draft_and_cannot_send():
    """Sending is an outward act with its own gate. A recipe that could send
    would be a second, ungated route to the same thing."""
    assert "mail_draft" in apps.RECIPES and "outlook_draft" in apps.RECIPES
    for recipe in apps.RECIPES.values():
        assert " send " not in recipe.script.lower()
        assert "send msg" not in recipe.script.lower()


def test_a_shortcut_that_says_nothing_is_not_reported_as_confirmed(monkeypatch):
    """A shortcut's effect cannot be read back, so its output IS the evidence."""
    assert "shortcut" in (apps.run_shortcut.__doc__ or "").lower()
    assert "run-but-silent" in (apps.run_shortcut.__doc__ or "")


def test_the_suite_cannot_reach_his_real_apps():
    """conftest points the interpreters at nothing, the same as the microphone
    and the voice server. A suite that writes to his Calendar is not a suite."""
    assert apps.OSASCRIPT == "/nonexistent/osascript"
    assert apps.SHORTCUTS == "/nonexistent/shortcuts"


def test_the_capability_reports_what_actually_happened(monkeypatch):
    from app import agent
    calls: list = []
    _fake_runner(monkeypatch, wrote=calls, reads="Nudge the reviewer")
    out = asyncio.run(agent.use_app("reminder_add", {"title": "Nudge the reviewer"}))
    assert out.startswith("Done in Reminders") and "read back" in out
    _fake_runner(monkeypatch, wrote=calls, reads="")
    bad = asyncio.run(agent.use_app("reminder_add", {"title": "Nudge the reviewer"}))
    assert bad.startswith("Not done —")
    assert "reminder_add" in agent.app_recipes()


def test_a_reading_recipe_hands_back_what_the_app_said(monkeypatch):
    """Found in the live proof: "what is on my reminders list" came back as the
    fact that it had been asked. A tool that returns only "ok" gets described
    to him instead of used."""
    from app import agent
    calls: list = []
    _fake_runner(monkeypatch, wrote=calls, reads="Buy milk\nChase the 1440 review")
    out = asyncio.run(apps.run("reminders_open"))
    assert "Chase the 1440 review" in out["said"]
    told = asyncio.run(agent.use_app("reminders_open"))
    assert "Buy milk" in told and "Chase the 1440 review" in told


def test_work_mail_goes_to_outlook_which_is_the_client_he_uses(monkeypatch):
    """Apple Mail sits empty on his Mac; his work mail is Outlook. A draft in the
    wrong client is a right mechanism aimed at an application he never opens —
    he would have found out by opening Mail and seeing nothing."""
    calls: list = []
    _fake_runner(monkeypatch, wrote=calls, reads="Kafka topics for UAT")
    out = asyncio.run(apps.run("outlook_draft", to="someone@example.com",
                               subject="Kafka topics for UAT", body="Draft only."))
    assert out["app"] == "Microsoft Outlook" and out["verified"] == "Kafka topics for UAT"
    assert "Microsoft Outlook" in calls[0]["script"]
    assert "someone@example.com" in calls[0]["args"]


def test_a_reminder_can_carry_the_time_he_said(monkeypatch):
    """Live on 17 Sep: "remind me tomorrow morning" put the reminder in his list
    with no time on it — the one thing that makes a reminder a reminder."""
    calls: list = []
    _fake_runner(monkeypatch, wrote=calls, reads="Chase Alex on the three PRs")
    out = asyncio.run(apps.run("reminder_add", title="Chase Alex on the three PRs",
                               due="2026-09-18 09:00"))
    assert out["ok"]
    assert "2026-09-18 09:00" in calls[0]["args"]
    assert "remind me date" in calls[0]["script"]
