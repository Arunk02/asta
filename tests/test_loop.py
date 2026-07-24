"""The autonomous conductor loop.

The design under test: a turn can leave behind a 'continue' signal (drive the next
step without waiting for Arun, bounded) or a 'send' signal (stage a draft and gate
on his confirmation — never auto-send). The state module is pure; the two tools
record intent for the conversation the turn belongs to.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from app import loop, agent, tasks, store


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    # Each test starts from a known env and no leftover conversation state.
    monkeypatch.delenv("ASTA_LOOP", raising=False)
    monkeypatch.delenv("ASTA_LOOP_MAX_STEPS", raising=False)
    for cid in ("c1", "c2"):
        loop.clear(cid)
    tasks.bind_conversation(None)
    yield
    for cid in ("c1", "c2"):
        loop.clear(cid)
    tasks.bind_conversation(None)


def test_enabled_defaults_on_and_respects_off(monkeypatch):
    assert loop.enabled() is True
    for off in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("ASTA_LOOP", off)
        assert loop.enabled() is False
    monkeypatch.setenv("ASTA_LOOP", "1")
    assert loop.enabled() is True


def test_max_steps_parses_and_falls_back(monkeypatch):
    assert loop.max_steps() == 4
    monkeypatch.setenv("ASTA_LOOP_MAX_STEPS", "7")
    assert loop.max_steps() == 7
    monkeypatch.setenv("ASTA_LOOP_MAX_STEPS", "garbage")
    assert loop.max_steps() == 4          # never crash the loop on a bad value


def test_continue_intent_round_trips_and_take_clears():
    loop.set_continue("c1", "read the log")
    got = loop.take("c1")
    assert got == {"kind": "continue", "next_step": "read the log"}
    assert loop.take("c1") is None        # read once, then gone


def test_send_intent_carries_target_and_channel():
    loop.set_pending_send("c1", "The fix is deployed.", to="#team", channel="teams")
    got = loop.take("c1")
    assert got["kind"] == "send"
    assert got["what"] == "The fix is deployed."
    assert got["to"] == "#team"
    assert got["channel"] == "teams"


def test_empty_channel_defaults_to_chat():
    loop.set_pending_send("c1", "hi", channel="")
    assert loop.take("c1")["channel"] == "chat"


def test_step_budget_bounds_autonomy(monkeypatch):
    monkeypatch.setenv("ASTA_LOOP_MAX_STEPS", "3")
    loop.reset_steps("c1")
    assert loop.budget_left("c1") is True
    for _ in range(3):
        loop.bump_steps("c1")
    assert loop.budget_left("c1") is False   # spent the budget → the loop must pause
    loop.reset_steps("c1")
    assert loop.budget_left("c1") is True     # a new user message refills it


def test_staged_send_is_held_then_cleared():
    intent = {"kind": "send", "what": "draft", "to": "", "channel": "chat"}
    loop.stage("c1", intent)
    assert loop.awaiting("c1") == intent
    assert loop.clear_awaiting("c1") == intent
    assert loop.awaiting("c1") is None


def test_clear_forgets_everything():
    loop.set_continue("c1", "x")
    loop.bump_steps("c1")
    loop.stage("c1", {"kind": "send"})
    loop.clear("c1")
    assert loop.take("c1") is None
    assert loop.budget_left("c1") is True
    assert loop.awaiting("c1") is None


def test_state_is_per_conversation():
    loop.set_continue("c1", "one")
    loop.set_continue("c2", "two")
    assert loop.take("c2")["next_step"] == "two"
    assert loop.take("c1")["next_step"] == "one"


# --- the two tools, called as the model would call them ----------------------

def test_continue_working_records_for_the_running_turn():
    tasks.bind_conversation("c1")
    out = agent.continue_working("verify the reminder fired")
    assert "verify the reminder fired" in out
    assert loop.take("c1") == {"kind": "continue", "next_step": "verify the reminder fired"}


def test_prepare_to_send_stages_and_never_claims_to_have_sent():
    tasks.bind_conversation("c1")
    out = agent.prepare_to_send("Deployed and green.", to="Priya", channel="teams")
    assert "confirm" in out.lower() and "sent" not in out.lower().split("confirm")[0]
    got = loop.take("c1")
    assert got["kind"] == "send" and got["to"] == "Priya"


def test_tools_no_op_without_a_conversation():
    tasks.bind_conversation(None)
    assert "No active conversation" in agent.continue_working("x")
    assert "No active conversation" in agent.prepare_to_send("x")


# --- the confirm-gate regex lives in main -----------------------------------

def test_affirm_regex_matches_yes_not_edits(monkeypatch):
    monkeypatch.setenv("ASTA_TOKEN", "t")
    monkeypatch.setenv("TEAMS_BRIDGE", "0")
    from app import main
    for yes in ("send", "send it", "yes", "y", "ok", "okay send", "go ahead", "confirm", "👍", "Yes."):
        assert main._AFFIRM.match(yes), yes
    for edit in ("change the greeting", "no, make it shorter", "send to Priya instead", "wait"):
        assert not main._AFFIRM.match(edit), edit


# --- the conductor itself: does the loop actually drive the work? ------------

class _Sink:
    def __init__(self):
        self.msgs: list[dict] = []

    async def send(self, m):
        self.msgs.append(m)

    def texts(self) -> str:
        return "\n".join(str(m.get("text", "")) for m in self.msgs)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db")
    store.init()
    monkeypatch.setenv("ASTA_TOKEN", "t")
    monkeypatch.setenv("TEAMS_BRIDGE", "0")


def test_conductor_auto_continues_then_stages_a_send(db, monkeypatch):
    """The model isn't done → the loop runs its next step itself (no user message),
    and when it drafts something outward the loop stops to stage it for confirmation."""
    from app import main
    conv = store.create_conversation(model="claude", workspace=None)
    cid = conv["id"]
    calls: list[str] = []
    script = iter([("continue", ("read the log",)),
                   ("send", ("all green", "Priya", "teams"))])

    async def fake_run_turn(sink, conv, text, channel):
        calls.append(text)
        try:
            kind, args = next(script)
        except StopIteration:
            return
        if kind == "continue":
            loop.set_continue(cid, args[0])
        else:
            loop.set_pending_send(cid, *args)

    monkeypatch.setattr(main, "_run_turn", fake_run_turn)
    sink = _Sink()
    asyncio.run(main._conducted_turn(conv, "start", sink, "web"))

    assert len(calls) == 2                      # the message, then ONE autonomous step
    assert "🔁 read the log" in sink.texts()     # the loop's thinking was shown
    assert loop.awaiting(cid) is not None        # the send is staged, waiting on him
    assert "can I send this?" in sink.texts()    # and he was asked, not auto-sent


def test_conductor_pauses_at_the_step_budget(db, monkeypatch):
    """A model that keeps asking to continue can't run away — the budget stops it."""
    monkeypatch.setenv("ASTA_LOOP_MAX_STEPS", "2")
    from app import main
    conv = store.create_conversation(model="claude", workspace=None)
    cid = conv["id"]
    calls: list[str] = []

    async def always_continue(sink, conv, text, channel):
        calls.append(text)
        loop.set_continue(cid, "keep going")

    monkeypatch.setattr(main, "_run_turn", always_continue)
    sink = _Sink()
    asyncio.run(main._conducted_turn(conv, "start", sink, "web"))

    assert len(calls) == 3                       # first message + exactly 2 auto-steps
    assert "paused" in sink.texts().lower()


def test_conductor_off_when_disabled(db, monkeypatch):
    """ASTA_LOOP=0 restores the old reactive behaviour — one turn, no continuation."""
    monkeypatch.setenv("ASTA_LOOP", "0")
    from app import main
    conv = store.create_conversation(model="claude", workspace=None)
    cid = conv["id"]
    calls: list[str] = []

    async def would_continue(sink, conv, text, channel):
        calls.append(text)
        loop.set_continue(cid, "next")

    monkeypatch.setattr(main, "_run_turn", would_continue)
    asyncio.run(main._conducted_turn(conv, "start", _Sink(), "web"))
    assert len(calls) == 1                        # stopped after one turn


# --- fix 1: CLI brains get their conv_id so the loop works for them too ------

def test_cli_orientation_hands_the_brain_its_conv_id_and_loop(db):
    """A CLI brain runs in a subprocess with no turn context, so the loop endpoints
    need conv_id — the orientation must supply it, or the loop is a no-op there."""
    from app import copilot_cli
    conv = store.create_conversation(model="copilot", workspace=None)
    ctx = copilot_cli._first_turn_context(conv, via="Copilot CLI", user_text="fix the bug")
    assert conv["id"] in ctx                       # it knows which conversation it is
    assert "/api/loop/continue" in ctx             # and how to drive itself
    assert "/api/loop/prepare-send" in ctx         # and to stage, not send


def test_cli_orientation_carries_the_skill_catalog(db, monkeypatch):
    """Parity: a CLI brain must see the same progressive-disclosure skill index the
    in-process brain gets — one line per skill, load the body on demand."""
    from app import copilot_cli, skills
    d = Path(tempfile.mkdtemp()) / "skills"
    d.mkdir()
    (d / "demo-skill.md").write_text(
        "---\nname: demo-skill\ndescription: A demo playbook for testing.\n---\n# Demo\nbody\n")
    monkeypatch.setattr(skills, "SKILLS_DIR", d)
    conv = store.create_conversation(model="copilot", workspace=None)
    ctx = copilot_cli._first_turn_context(conv, user_text="hi")
    assert "demo-skill" in ctx                      # the catalog is present
    assert "load_skill" in ctx                      # and it's told how to pull the body


# --- fix 2: extended thinking is a real switch, off by default --------------

def test_thinking_is_off_by_default_and_switchable(monkeypatch):
    monkeypatch.delenv("ASTA_THINKING", raising=False)
    assert agent.thinking_budget() == 0
    assert "anthropic_thinking" not in agent.model_settings("claude")

    monkeypatch.setenv("ASTA_THINKING", "on")
    assert agent.thinking_budget() == 2048
    assert agent.model_settings("claude")["anthropic_thinking"] == {
        "type": "enabled", "budget_tokens": 2048}

    monkeypatch.setenv("ASTA_THINKING", "4096")
    assert agent.model_settings("claude")["anthropic_thinking"]["budget_tokens"] == 4096

    monkeypatch.setenv("ASTA_THINKING", "junk")
    assert agent.thinking_budget() == 0            # a bad value never crashes a turn
    assert agent.model_settings("nonclaude") is None
