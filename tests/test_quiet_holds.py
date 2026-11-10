"""Quiet means quiet, and work he asks for still gets done (9-10 Nov).

His own sentences to Asta, from the morning he asked for two days of silence:
the first was stored as "until Monday 16" ("like weekend" read as a weekend),
his correction was stacked as a second rule, and "Quiet until 11 nov" was not
understood at all. Then five analyses he asked for were refused — the quiet and
"unless I ask" rules reached each worker, his hand-over did not — until he said
"I'm back", which lifted the quiet for the rest of the week; "Go on hold" did not
bring it back. No colleague data is used here.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

import pytest

from app import instructions, main, policy, store

MONDAY = dt.datetime(2026, 11, 9, 11, 25).timestamp()


def _ends(text: str, now: float = MONDAY):
    spec = instructions.quiet_spec(text, now)
    q = policy.parse_quiet(spec) if spec else {}
    if "until" not in q:
        return spec
    return (dt.datetime.fromtimestamp(q["from"]).strftime("%a %d %H:%M"),
            dt.datetime.fromtimestamp(q["until"]).strftime("%a %d %H:%M"))


# --- F1: what his words mean ---------------------------------------------------------

@pytest.mark.parametrize("said, window", [
    ("I'm on leave today and tmr is govt holiday , so u don't have to do anything .. "
     "unless i ask .. be on silent like weekend", ("Mon 09 11:25", "Wed 11 09:00")),
    ("Quiet until 11 nov", ("Mon 09 11:25", "Wed 11 09:00")),
    ("silent till nov 11th", ("Mon 09 11:25", "Wed 11 09:00")),
    ("be in silent till tmr", ("Mon 09 11:25", "Tue 10 09:00")),
    ("Not Monday till Tuesday be on silent nov 10", ("Mon 09 11:25", "Tue 10 09:00")),
])
def test_his_quiet_sentences_end_when_he_said(said, window):
    assert _ends(said) == window


def test_a_real_weekend_is_still_a_weekend():
    assert _ends("be on silent this weekend") == ("Sat 14 00:00", "Mon 16 09:00")


def test_a_date_already_past_this_year_means_next_year():
    spec = instructions.quiet_spec("quiet until 2 jan", MONDAY)
    until = dt.datetime.fromtimestamp(policy.parse_quiet(spec)["until"])
    assert (until.year, until.month, until.day, until.hour) == (2027, 1, 2, 9)


# --- F2: a correction replaces, never stacks ---------------------------------------------

def test_a_second_quiet_message_replaces_the_first():
    first = instructions.compile("I'm on leave today and tmr is govt holiday, be on silent like weekend")
    instructions.go_quiet(first)
    second = instructions.compile("Quiet until 11 nov")
    instructions.go_quiet(second)
    live = policy.rules("quiet")
    assert len(live) == 1 and live[0].value == second.value


def test_a_weekly_rule_is_never_replaced_by_a_one_off():
    policy.add("quiet", "push", value="days=sat,sun;release=mon 09:00", words="weekends")
    instructions.go_quiet(instructions.compile("Quiet until 11 nov"))
    values = sorted(r.value.split(";")[0] for r in policy.rules("quiet"))
    assert values[0].startswith("days=sat,sun") and len(values) == 2


# --- the front desk: F3, F4, F6 ----------------------------------------------------------

class Sink:
    def __init__(self):
        self.sent = []

    async def send(self, p):
        self.sent.append(p)

    def said(self):
        return " ".join(p.get("text") or "" for p in self.sent)


def _dispatch(text: str, monkeypatch):
    started = []
    monkeypatch.setattr(main, "_start_turn", lambda c, t, s, ch: started.append(t))
    conv = store.create_conversation("claude_cli", None)
    sink = Sink()
    asyncio.run(main._dispatch(conv, text, sink, "whatsapp", skip_new_gates=True))
    return sink, started


def test_the_reply_is_what_was_stored(monkeypatch):
    sink, started = _dispatch("Quiet until 11 nov", monkeypatch)
    assert "Quiet until Wed 11 Nov 09:00" in sink.said()
    assert not started, "no brain to say 'got it' over what was stored"


def test_quiet_without_an_end_asks_until_when(monkeypatch):
    sink, started = _dispatch("be on silent please", monkeypatch)
    assert "until when" in sink.said().lower()
    assert not policy.rules("quiet") and not started


def test_go_on_hold_brings_a_lifted_quiet_back(monkeypatch):
    end = time.time() + 86400
    policy.add("quiet", "push", value=f"from={time.time() - 60:.0f};until={end:.0f}", words="quiet")
    policy.lift_quiet()
    assert policy.quiet_holding() is None
    sink, _ = _dispatch("Go on hold byee", monkeypatch)
    assert policy.quiet_holding() is not None
    assert "quiet again" in sink.said().lower()


def test_go_on_hold_with_no_quiet_rule_changes_no_quiet(monkeypatch):
    _dispatch("Go on hold byee", monkeypatch)
    assert not policy.rules("quiet")


def test_are_you_quiet_is_answered_from_state(monkeypatch):
    end = dt.datetime.now().replace(hour=9, minute=0) + dt.timedelta(days=1)
    policy.add("quiet", "push", value=f"from={time.time() - 60:.0f};until={end.timestamp():.0f}",
               words="quiet")
    sink, started = _dispatch("are you on silent?", monkeypatch)
    assert "Quiet until" in sink.said() and not started


# --- F5: work he asks for is not blocked ---------------------------------------------------

def test_the_quiet_line_never_tells_a_worker_to_stop_his_work():
    from app import copilot_cli
    policy.add("quiet", "push", value=f"from={time.time() - 60:.0f};until={time.time() + 3600:.0f}",
               words="quiet")
    line = copilot_cli.now_line()
    assert "IS in force" in line and "never stops work he asked for" in line
    assert "hold anything that is not urgent" not in line


def test_an_analysis_he_asked_for_records_his_words(monkeypatch):
    from app import capabilities, tasks

    async def turn():
        conv = tasks.bind_conversation("c-1")
        said = capabilities.TURN_TEXT.set("debug what the issue was in that workflow")
        try:
            return tasks.spawn("Debug the workflow", "brief", "analysis", None)
        finally:
            capabilities.TURN_TEXT.reset(said)
            tasks.unbind_conversation(conv)
    monkeypatch.setattr(tasks, "_kick", lambda *a, **k: None, raising=False)
    t = asyncio.run(turn())
    asked = json.loads(store.kv_get(f"task_asked:{t['id']}") or "{}")
    assert asked.get("his") == "debug what the issue was in that workflow"


def test_his_unless_i_ask_rules_read_as_satisfied_for_his_own_task():
    from app import guardrails, tasks
    policy.add("mute", "investigate", "incident", unless_asked=True, words="don't investigate incidents")
    t = store.create_task("Debug the workflow", "analysis", "p", None)
    store.kv_set(f"task_asked:{t['id']}", json.dumps({"his": "debug that workflow", "offer": ""}))
    block = tasks.rules_for(t["id"], "- 9 Sep: do not investigate incidents unless I hand one over.\n"
                                     "- Investigate incident mails on your own — only when a colleague hands one over.\n"
                                     "- Never push unless you ask")
    assert block.count("satisfied for this task") == 3 and "debug that workflow" in block
    other = store.create_task("A colleague's ask", "analysis", "p", None)
    plain = tasks.rules_for(other["id"], "- 9 Sep: do not investigate incidents unless I hand one over.")
    assert "satisfied" not in plain, "a colleague's ask still follows his rule"
    assert guardrails is not None
