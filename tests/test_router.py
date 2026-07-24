"""The brain-router must divert ONLY pleasantries — a real request downgraded to the
local model is the expensive mistake — and a diverted turn must never spawn a paid CLI.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from app import router, store


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


# --- classification: the whole safety story ---------------------------------

@pytest.mark.parametrize("txt", [
    "hi", "Hey!", "hello.", "good morning", "gm", "thanks", "thank you",
    "thx", "cheers", "great", "perfect", "ok", "okay", "got it", "sounds good",
    "bye", "see ya", "good night",
])
def test_pleasantries_are_trivial(txt):
    assert router.is_trivial(txt)


@pytest.mark.parametrize("txt", [
    "",
    "hi, can you check the booking logs",       # greeting + real ask
    "thanks for nothing, why did it fail",       # word present but not alone
    "okay so here is the plan for the refactor",
    "great, now open a PR",
    "status",                                    # a real (if cheap) query, not social
    "why is the service slow",
])
def test_real_content_is_not_trivial(txt):
    assert not router.is_trivial(txt)


# --- reply: local model when up, canned when not ----------------------------

def test_reply_prefers_the_local_model(monkeypatch):
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "Hello there!")
    assert asyncio.run(router.reply("hi")) == "Hello there!"


def test_reply_falls_back_to_canned_when_local_is_down(monkeypatch):
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)
    assert asyncio.run(router.reply("thanks")) == "Anytime! 👍"
    assert asyncio.run(router.reply("bye")) == "Catch you later! 👋"


def test_disabled_router_is_a_no_op(monkeypatch):
    monkeypatch.setenv("ASTA_ROUTER", "0")
    assert router.enabled() is False


# --- the hot path: a greeting must not spawn a paid brain -------------------

def test_run_turn_answers_a_greeting_without_a_paid_brain(db, monkeypatch):
    from app import main, memory
    monkeypatch.setenv("ASTA_ROUTER", "1")
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)   # force canned

    async def boom(*a, **k):
        raise AssertionError("a paid brain was spawned to answer 'thanks'")

    monkeypatch.setattr(main, "_run_turn_cli", boom)
    monkeypatch.setattr(main, "_run_turn_streaming", boom)
    conv = store.create_conversation(model="copilot", workspace=None)
    sink = _Sink()
    asyncio.run(main._run_turn(sink, dict(conv), "thanks!", "web"))
    assert "Anytime" in sink.texts()
    assert any(m.get("type") == "done" for m in sink.msgs)
    saved = store.list_ui_messages(conv["id"])
    assert any(m["role"] == "assistant" for m in saved)   # persisted, via the router
