"""A chat session is retired by SIZE, not only by idleness.

Measured on 11 September: the WhatsApp conversation's Claude session carried
232k tokens of context into every call. Median reply 60 s, of which 48 s passed
before the first word — the model re-reading the session, not thinking about
the question — and eight analysis tasks failed on the five-hour window that
those re-reads were draining. The idle digest could never have caught it: it
retires a thread after thirty quiet minutes, and that thread never has thirty
quiet minutes. And `digested` was set once and never cleared, so even a quiet
morning would not have digested it twice.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import copilot_cli, llm_meter, main, store


# --- the measurement -----------------------------------------------------------

def test_context_is_the_largest_call_not_the_sum():
    """A ten-step turn on a 50k session and a one-step turn on a 500k session
    SUM alike. Only the per-call maximum says how big the session has grown."""
    a = llm_meter.from_anthropic({"input_tokens": 2, "cache_read_input_tokens": 90_000,
                                  "cache_creation_input_tokens": 500, "output_tokens": 10})
    b = llm_meter.from_anthropic({"input_tokens": 2, "cache_read_input_tokens": 91_000,
                                  "output_tokens": 10})
    assert a.context == 90_502
    total = a + b
    assert total.context == 91_002               # a maximum
    assert total.cache_read == 181_000           # the spend is still a sum
    assert "context" in total.as_dict()


def test_copilot_reports_the_context_it_carries(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_cli, "COPILOT_SESSIONS", tmp_path)
    store.kv_set("copilot_session:c1", "s1")
    (tmp_path / "s1").mkdir()
    (tmp_path / "s1" / "events.jsonl").write_text(
        json.dumps({"type": "session.shutdown", "data": {"currentTokens": 41_000}}) + "\n")
    u = copilot_cli.last_turn_usage({"id": "c1"}, reply_chars=400)
    assert u.context == 41_000 and u.measured


# --- the retirement ------------------------------------------------------------

class _Sink:
    async def send(self, payload):
        pass


def _brain(context: int):
    class Cli:
        @staticmethod
        async def run_turn(conv, text, on_delta, on_usage=None):
            if on_usage:
                on_usage(llm_meter.Usage(input=2, cache_read=context, context=context,
                                         measured=True))
            await on_delta("ok")
            return "ok"
    return Cli


@pytest.fixture
def quiet_digest(monkeypatch):
    """Keep the digest out of the event loop; record that it was scheduled."""
    scheduled: list[str] = []

    def once(name, coro):
        scheduled.append(name)
        coro.close()

    monkeypatch.setattr(main.daemon, "once", once)
    return scheduled


def test_a_call_over_the_cap_retires_the_session_before_the_next_message(monkeypatch, quiet_digest):
    monkeypatch.setenv("ASTA_SESSION_MAX_TOKENS", "100000")
    conv = store.create_conversation("claude_cli", None)
    store.kv_set(f"claude_session:{conv['id']}", "sid-grown")
    asyncio.run(main._run_turn_cli(_Sink(), conv, "hi", _brain(150_000), "claude_cli",
                                   channel="whatsapp"))
    assert not (store.kv_get(f"claude_session:{conv['id']}") or "").strip()
    assert store.kv_get(f"session_recap:{conv['id']}") == "1"
    assert quiet_digest == [f"digest:{conv['id']}"]
    rows = [r for r in store.recent_outcomes(20) if r["kind"] == "session"]
    assert rows and "context=150000" in rows[0]["detail"]


def test_a_call_under_the_cap_keeps_the_session(monkeypatch, quiet_digest):
    monkeypatch.setenv("ASTA_SESSION_MAX_TOKENS", "100000")
    conv = store.create_conversation("claude_cli", None)
    store.kv_set(f"claude_session:{conv['id']}", "sid-fine")
    asyncio.run(main._run_turn_cli(_Sink(), conv, "hi", _brain(60_000), "claude_cli"))
    assert store.kv_get(f"claude_session:{conv['id']}") == "sid-fine"
    assert store.kv_get(f"session_recap:{conv['id']}") is None
    assert quiet_digest == []


def test_zero_disables_it(monkeypatch, quiet_digest):
    monkeypatch.setenv("ASTA_SESSION_MAX_TOKENS", "0")
    conv = store.create_conversation("claude_cli", None)
    store.kv_set(f"claude_session:{conv['id']}", "sid-kept")
    asyncio.run(main._run_turn_cli(_Sink(), conv, "hi", _brain(900_000), "claude_cli"))
    assert store.kv_get(f"claude_session:{conv['id']}") == "sid-kept"


# --- the thread continues, it does not restart --------------------------------

def test_the_fresh_session_is_handed_a_recap_once():
    conv = store.create_conversation("claude_cli", None)
    store.add_ui_message(conv["id"], "user", "is the booking PR merged?")
    store.add_ui_message(conv["id"], "assistant", "Not yet — CI is still running.")
    store.kv_set(f"session_recap:{conv['id']}", "1")
    recap = copilot_cli._switch_recap(conv, "Claude Code CLI")
    assert "Arun: is the booking PR merged?" in recap
    assert "retired" in recap and "same conversation" in recap
    # Consumed: the next system prompt must not carry it again.
    assert store.kv_get(f"session_recap:{conv['id']}") is None
    assert copilot_cli._switch_recap(conv, "Claude Code CLI") == ""


def test_a_brain_switch_still_recaps_as_before():
    conv = store.create_conversation("copilot", None)
    store.add_ui_message(conv["id"], "user", "what did vinish say")
    store.add_ui_message(conv["id"], "assistant", "He asked about the retry fix.")
    store.add_ui_message(conv["id"], "user", "and now?")      # the current turn, dropped
    store.kv_set(f"copilot_session:{conv['id']}", "live-elsewhere")
    recap = copilot_cli._switch_recap(conv, "Claude Code CLI")
    assert "model switch" in recap and "what did vinish say" in recap
    assert "and now?" not in recap


# --- and the idle digest can fire again ---------------------------------------

def test_a_new_message_makes_the_conversation_digestible_again():
    conv = store.create_conversation("claude_cli", None)
    store.update_conversation(conv["id"], digested=1)
    store.add_ui_message(conv["id"], "user", "one more thing")
    assert store.get_conversation(conv["id"])["digested"] == 0
    assert conv["id"] in {c["id"] for c in store.stale_undigested_conversations(idle_seconds=-1)}
