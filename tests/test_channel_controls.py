"""The controls he can reach from his phone.

Everything here exists because the web UI had a dropdown and WhatsApp had nothing.
The quota warning arrives on his phone; the model picker was on his laptop; so the
one place he could read that a brain had run dry was the one place he could not do
anything about it.

The second theme is precedence. Four separate mechanisms can own his next message
— a staged send, an offer, an open question, a live task — and a one-word answer
to an invisible question is a coin flip. These pin which one a "yes" reaches, and
that the meta-commands do not eat an answer meant for something else.
"""

from __future__ import annotations

import asyncio

import pytest

from app import agent as agent_mod
from app import main, offers, resume, store


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    offers.clear()
    yield


class _Sink:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, payload):
        self.sent.append(payload)


def _conv(model="copilot"):
    c = store.create_conversation(model=model, workspace=None)
    c["model"] = model
    return c


def _reg(monkeypatch, **avail):
    monkeypatch.setattr(agent_mod, "model_registry", lambda: {
        name: {"label": name.title(), "available": ok, "detail": "" if ok else "add a key"}
        for name, ok in avail.items()})


def _say(conv, text, sink=None, channel="whatsapp"):
    return asyncio.run(main._dispatch(conv, text, sink or _Sink(), channel))


# --- switching brains -------------------------------------------------------

def test_switching_from_whatsapp_actually_sticks(monkeypatch):
    _reg(monkeypatch, copilot=True, claude_cli=True)
    conv = _conv("copilot")
    sink = _Sink()
    assert _say(conv, "use claude_cli", sink) is None
    assert conv["model"] == "claude_cli"
    assert store.get_conversation(conv["id"])["model"] == "claude_cli"   # persisted
    assert any("claude" in str(p).lower() for p in sink.sent)


@pytest.mark.parametrize("phrase", ["use claude_cli", "switch to claude_cli",
                                    "swap to claude_cli", "change to claude_cli"])
def test_the_ways_he_actually_phrases_it(monkeypatch, phrase):
    _reg(monkeypatch, copilot=True, claude_cli=True)
    conv = _conv("copilot")
    _say(conv, phrase)
    assert conv["model"] == "claude_cli"


def test_a_partial_name_resolves_when_it_is_unambiguous(monkeypatch):
    _reg(monkeypatch, copilot=True, claude_cli=True)
    conv = _conv("copilot")
    _say(conv, "use copilot")
    assert conv["model"] == "copilot"


def test_an_unknown_brain_lists_the_real_ones_instead_of_guessing(monkeypatch):
    _reg(monkeypatch, copilot=True, claude_cli=True)
    conv = _conv("copilot")
    sink = _Sink()
    _say(conv, "use gpt5", sink)
    said = " ".join(str(p) for p in sink.sent)
    assert "don't have a brain" in said and "copilot" in said
    assert conv["model"] == "copilot"                    # unchanged


def test_switching_to_something_unconfigured_warns_but_obeys(monkeypatch):
    """Refusing a choice he stated is more annoying than a warning he can ignore —
    he may be about to add the key."""
    _reg(monkeypatch, copilot=True, openai=False)
    conv = _conv("copilot")
    sink = _Sink()
    _say(conv, "use openai", sink)
    assert conv["model"] == "openai"
    assert "isn't configured" in " ".join(str(p) for p in sink.sent)


def test_listing_marks_the_one_answering_right_now(monkeypatch):
    _reg(monkeypatch, copilot=True, claude_cli=False)
    conv = _conv("copilot")
    sink = _Sink()
    _say(conv, "which model?", sink)
    said = " ".join(str(p) for p in sink.sent)
    assert "→ copilot" in said
    assert "add a key" in said                            # says WHY one is unusable


def test_a_phone_channel_honours_his_choice(monkeypatch):
    """The regression: both phone channels pinned the model to the default on
    every message, so 'use claude' appeared to work and answered on Copilot."""
    _reg(monkeypatch, copilot=True, claude_cli=True)
    monkeypatch.setattr(agent_mod, "default_chat_model", lambda: "copilot")
    assert main._channel_model({"model": "claude_cli"}) == "claude_cli"


def test_a_choice_that_has_since_broken_falls_back_rather_than_failing(monkeypatch):
    """He closed LM Studio. Answering on something that works beats not answering."""
    _reg(monkeypatch, copilot=True, local=False)
    monkeypatch.setattr(agent_mod, "default_chat_model", lambda: "copilot")
    assert main._channel_model({"model": "local"}) == "copilot"
    assert main._channel_model({}) == "copilot"


# --- switching does not answer a question -----------------------------------

def test_switching_leaves_an_open_offer_standing(monkeypatch):
    """Picking a different brain is not a change of subject, and must not drop the
    question he was about to say yes to."""
    _reg(monkeypatch, copilot=True, claude_cli=True)
    offers.for_ci_failure("Arunk02/asta", "build.yml", "main", "https://gh/1")
    _say(_conv("copilot"), "use claude_cli")
    assert offers.pending() is not None


def test_switching_after_a_quota_stop_carries_on_by_itself(monkeypatch):
    """This is the whole reason he asked for phone-side switching — making him
    then type 'resume' would be theatre."""
    _reg(monkeypatch, copilot=True, claude_cli=True)
    started = {}
    monkeypatch.setattr(main, "_start_turn",
                        lambda c, p, s, ch: started.update(prompt=p) or "task")
    conv = _conv("copilot")
    resume.save(conv["id"], "fix the login test", "copilot", partial="It's the refresh.")
    assert _say(conv, "use claude_cli") == "task"
    assert "fix the login test" in started["prompt"]
    assert "It's the refresh" in started["prompt"]
    assert resume.get(conv["id"]) is None                # consumed, not repeatable


def test_switching_to_the_same_brain_does_not_replay_parked_work(monkeypatch):
    _reg(monkeypatch, copilot=True)
    monkeypatch.setattr(main, "_start_turn",
                        lambda *a: pytest.fail("no switch happened; nothing should run"))
    conv = _conv("copilot")
    resume.save(conv["id"], "req", "copilot")
    _say(conv, "use copilot")
    assert resume.get(conv["id"]) is not None            # still parked


# --- resuming ---------------------------------------------------------------

def test_resume_picks_up_parked_work(monkeypatch):
    started = {}
    monkeypatch.setattr(main, "_start_turn",
                        lambda c, p, s, ch: started.update(prompt=p) or "task")
    conv = _conv()
    resume.save(conv["id"], "the original ask", "copilot")
    assert _say(conv, "resume") == "task"
    assert "the original ask" in started["prompt"]


def test_resume_with_nothing_parked_falls_through_to_a_normal_turn(monkeypatch):
    """The trap: 'carry on' is also what he says to the conductor loop's pause.
    Answering 'nothing to resume' would eat a perfectly ordinary message."""
    started = {}
    monkeypatch.setattr(main, "_start_turn",
                        lambda c, p, s, ch: started.update(prompt=p) or "task")
    conv = _conv()
    assert _say(conv, "carry on") == "task"
    assert started["prompt"] == "carry on"               # reached the brain verbatim


def test_a_bare_continue_is_left_to_the_loop(monkeypatch):
    started = {}
    monkeypatch.setattr(main, "_start_turn",
                        lambda c, p, s, ch: started.update(prompt=p) or "task")
    conv = _conv()
    resume.save(conv["id"], "parked work", "copilot")
    _say(conv, "continue")
    assert started["prompt"] == "continue"               # not hijacked into a resume


# --- knowing what is waiting ------------------------------------------------

def test_pending_lists_things_in_the_order_a_yes_would_reach_them(monkeypatch):
    conv = _conv()
    offers.propose("PROJ-1", "ctx", "Implement it?", "implement PROJ-1")
    sink = _Sink()
    _say(conv, "what's pending?", sink)
    said = " ".join(str(p) for p in sink.sent)
    assert "PROJ-1" in said
    assert "implement PROJ-1" in said                    # says what yes actually means


def test_pending_spells_out_what_a_staged_write_would_do(monkeypatch):
    conv = _conv()
    offers.staged_write("jira_comment", {"key": "PROJ-9", "text": "x"},
                        "s", "c", "Post it?")
    sink = _Sink()
    _say(conv, "anything waiting on me", sink)
    assert "Comment on PROJ-9" in " ".join(str(p) for p in sink.sent)


def test_pending_says_so_plainly_when_nothing_is_waiting():
    sink = _Sink()
    _say(_conv(), "pending", sink)
    assert "Nothing is waiting" in " ".join(str(p) for p in sink.sent)


def test_asking_what_is_pending_does_not_answer_the_pending_thing():
    """It is a question about the question, not an answer to it."""
    offers.propose("s", "c", "Do it?", "do it")
    _say(_conv(), "what's pending?")
    assert offers.pending() is not None
