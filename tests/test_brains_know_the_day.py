"""Every brain must be told what day it is — especially the ones nobody is watching.

His report, Monday 28 Sep: *"some of the tasks are not doing thinking it is
sunday"*. The chain is short and entirely self-inflicted:

  1. `guardrails.block()` hands every brain his standing rule, verbatim and
     frozen: "2026-09-19: Quiet on Saturdays and Sundays — one summary Monday
     09:00". It is a CONDITION, written as prose, with no way to evaluate it.
  2. `run_turn` stamps the local time on each chat turn. `one_shot` — the
     entry point for every background brain call: tasks, the responder,
     digests, triage, plans, reviews — stamped nothing at all.
  3. So a background brain reads "quiet on Saturdays and Sundays", has no idea
     what day it is, guesses, and holds back. "Not doing", politely.

The fix is not to hope it guesses right. It is to stop asking it to infer policy
at all: say the date, and say whether the rule is in force, in ONE place that
every brain entry point goes through.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app import claude_cli, copilot_cli, policy

#: Captured at IMPORT time, before conftest's autouse fixture replaces `one_shot`
#: with a raiser to stop a test billing a real brain. These tests are about what
#: the real function puts in front of a model, so they need the real function —
#: which conftest itself says is the way to ask for it.
_REAL = {claude_cli: {"one_shot": claude_cli.one_shot, "run_turn": claude_cli.run_turn},
         copilot_cli: {"one_shot": copilot_cli.one_shot, "run_turn": copilot_cli.run_turn}}


# --- what the brain is told about when it is ----------------------------------

def test_the_line_names_today():
    line = copilot_cli.now_line()
    today = dt.datetime.now()
    assert today.strftime("%Y-%m-%d") in line
    assert today.strftime("%a") in line, "the weekday is the part that was missing"


def test_a_weekend_rule_is_reported_as_not_in_force_on_a_monday():
    """The whole bug: the rule is real, and today is not one of its days."""
    policy.add("quiet", "push", value="days=sat,sun;release=mon 09:00",
               words="weekends off, summarise Monday")
    monday = dt.datetime(2026, 9, 28, 12, 40).timestamp()
    line = copilot_cli.now_line(monday)
    assert "Mon" in line
    assert "NOT in force" in line, (
        "a brain that has to infer this from a frozen sentence infers it wrong")


def test_the_same_rule_is_reported_as_in_force_on_a_sunday():
    policy.add("quiet", "push", value="days=sat,sun;release=mon 09:00",
               words="weekends off, summarise Monday")
    sunday = dt.datetime(2026, 9, 27, 12, 40).timestamp()
    line = copilot_cli.now_line(sunday)
    assert "in force" in line and "NOT in force" not in line


def test_no_quiet_rule_means_nothing_extra_is_said():
    line = copilot_cli.now_line()
    assert "in force" not in line, "do not invent policy that does not exist"


# --- the seam: every entry point, not just the chat one -----------------------

def _fake_exec(captured: list[list[str]]):
    class _Proc:
        returncode = 0

        class stdout:
            _done = False

            @staticmethod
            async def read(_n):
                if _Proc.stdout._done:
                    return b""
                _Proc.stdout._done = True
                return b"done"

        @staticmethod
        async def wait():
            return 0

    async def fake(*cmd, **kw):
        captured.append(list(cmd))
        _Proc.stdout._done = False
        return _Proc()
    return fake


@pytest.mark.parametrize("mod", [claude_cli, copilot_cli], ids=["claude", "copilot"])
def test_a_background_brain_call_carries_the_day(mod, monkeypatch):
    """`one_shot` is how a task, a digest, a triage and a review all reach a
    brain. Every one of them ran date-blind."""
    seen: list[list[str]] = []
    monkeypatch.setattr(mod, "available", lambda: True)
    monkeypatch.setattr(mod, "one_shot", _REAL[mod]["one_shot"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec(seen))
    asyncio.run(mod.one_shot("Summarise what came in and decide what needs him."))
    assert seen, "no subprocess was started"
    argv = " ".join(seen[0])
    assert dt.datetime.now().strftime("%a") in argv, (
        f"{mod.__name__}.one_shot told the brain nothing about what day it is")


@pytest.mark.parametrize("mod", [claude_cli, copilot_cli], ids=["claude", "copilot"])
def test_a_chat_turn_carries_the_day_too(mod, monkeypatch):
    """The chat path already did this, with its own copy of the string in each
    file. Asserted here so the two cannot drift.

    Through `_build_cmd`, not `run_turn`: building the command is the part that
    decides what the model is told, and it starts no subprocess, opens no CLI
    session and leaves no sticky tool selection behind for the next test.
    """
    from app import tool_index
    was = dict(tool_index._sticky)
    try:
        cmd = mod._build_cmd({"id": "conv-day-test", "model": "x", "workspace": None},
                             "what is on for me today?")
    finally:
        tool_index._sticky.clear()
        tool_index._sticky.update(was)
    assert dt.datetime.now().strftime("%a") in " ".join(cmd), (
        f"{mod.__name__} builds a chat turn that never says what day it is")


def test_the_stamp_has_exactly_one_definition():
    """A guard that fails by name when a new caller writes its own `[now: …]`.

    The string was already copied into two files before this; a third copy is
    how "the chat brain knows the day and the task brain does not" happens
    again. Only now_line may spell it.
    """
    import pathlib as _p
    import re as _re
    # An f-string BUILDING the stamp. `scorecard` matches "[now:" in a regex to
    # recognise a row Asta wrote itself — a reader, not a second definition.
    writes_it = _re.compile(r'f["\'][^"\']*\[now:')
    offenders = []
    for path in sorted(_p.Path("app").rglob("*.py")):
        if path.name == "copilot_cli.py":
            continue                        # now_line itself lives here
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if writes_it.search(line):
                offenders.append(f"{path}:{i}")
    assert not offenders, (
        "these spell the time stamp themselves instead of calling "
        "copilot_cli.now_line(): " + ", ".join(offenders))
