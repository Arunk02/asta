"""Picking work up where a brain ran out.

The scenario these are all about: Copilot's credits run out twelve minutes into a
real task. What used to happen is that the original message got replayed on
another brain, which started from nothing and paid again for everything the first
one had already worked out — and if no brain was left, the request simply
vanished with the turn.

So the properties worth pinning: the checkpoint carries what was ESTABLISHED, not
the request; a handoff continues rather than restarts; nothing is lost when there
is nobody to hand to; and a day-old checkpoint does not ambush him.
"""

from __future__ import annotations

import asyncio

import pytest

from app import main, resume, store


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    monkeypatch.delenv("ASTA_RESUME_TTL", raising=False)
    yield


# --- the checkpoint ---------------------------------------------------------

def test_a_checkpoint_keeps_what_was_established_not_just_the_request():
    resume.save("c1", "fix the failing login test", "copilot",
                partial="The token refresh races on startup.")
    point = resume.get("c1")
    assert point["request"] == "fix the failing login test"
    assert "races on startup" in point["partial"]
    assert point["brain"] == "copilot"


def test_only_the_tail_of_a_long_partial_is_kept():
    """The conclusion is at the end. The beginning is context the next brain can
    rebuild from the repo more cheaply than from a transcript."""
    resume.save("c1", "req", "copilot", partial=("x" * 9000) + "THE ANSWER")
    point = resume.get("c1")
    assert point["partial"].endswith("THE ANSWER")
    assert len(point["partial"]) <= resume.MAX_PARTIAL


def test_a_checkpoint_survives_a_restart():
    resume.save("c1", "req", "copilot")
    assert store.kv_get(f"{resume.KEY}:c1")          # in the DB, not in memory


def test_a_stale_checkpoint_does_not_ambush_him(monkeypatch):
    """A day later the branch has moved and so has he. Resuming would produce
    confident work against a world that no longer exists."""
    monkeypatch.setenv("ASTA_RESUME_TTL", "3600")
    resume.save("c1", "req", "copilot")
    assert resume.get("c1") is not None
    monkeypatch.setattr(resume.time, "time", lambda: 10**10)
    assert resume.get("c1") is None


def test_ttl_zero_keeps_it_forever(monkeypatch):
    monkeypatch.setenv("ASTA_RESUME_TTL", "0")
    resume.save("c1", "req", "copilot")
    monkeypatch.setattr(resume.time, "time", lambda: 10**10)
    assert resume.get("c1") is not None


def test_checkpoints_are_per_conversation():
    resume.save("c1", "one", "copilot")
    resume.save("c2", "two", "claude_cli")
    assert resume.get("c1")["request"] == "one"
    assert resume.get("c2")["request"] == "two"


def test_corrupt_state_never_crashes():
    store.kv_set(f"{resume.KEY}:c1", "{not json")
    assert resume.get("c1") is None
    store.kv_set(f"{resume.KEY}:c1", '{"brain": "copilot"}')       # no request
    assert resume.get("c1") is None


# --- the handoff itself -----------------------------------------------------

def test_the_handoff_says_continue_not_start_over():
    point = resume.save("c1", "fix the login test", "copilot",
                        partial="Narrowed it to the token refresh.")
    p = resume.handoff_prompt(point, "claude_cli")
    assert "fix the login test" in p
    assert "Narrowed it to the token refresh" in p
    assert "not starting it over" in p
    assert "Do not redo work" in p


def test_the_partial_is_offered_as_a_lead_not_as_fact():
    """It came from a different model and was cut off mid-thought. Presenting it
    as settled turns one brain's guess into two brains' certainty."""
    point = resume.save("c1", "req", "copilot", partial="Probably the cache.")
    p = resume.handoff_prompt(point, "claude_cli")
    assert "verify" in p.lower()


def test_a_handoff_with_nothing_produced_still_reads_sensibly():
    point = resume.save("c1", "req", "copilot", partial="")
    p = resume.handoff_prompt(point, "claude_cli")
    assert "req" in p
    assert "———" not in p            # no empty evidence block


def test_the_note_says_why_it_stopped_in_his_terms():
    """Copilot's is a monthly pool and Claude's a rolling window — which one it was
    decides whether he tops up or just waits."""
    point = resume.save("c1", "req", "claude_cli", why="Claude subscription limit hit")
    assert "subscription limit" in resume.note(point, "copilot")
    assert "copilot" in resume.note(point, "copilot")


def test_the_note_mentions_that_context_carried_over_only_when_it_did():
    with_partial = resume.save("c1", "r", "copilot", partial="something")
    without = resume.save("c2", "r", "copilot", partial="")
    assert "carrying over" in resume.note(with_partial, "claude_cli")
    assert "carrying over" not in resume.note(without, "claude_cli")


def test_the_parked_note_names_both_ways_out():
    """Nothing can take over. He needs to know the work is kept and what to do —
    reporting a failure alone would make him retype the request."""
    point = resume.save("c1", "req", "copilot", why="Copilot quota exhausted")
    text = resume.parked_note(point)
    assert "resume" in text.lower() and "use <brain>" in text
    assert "quota exhausted" in text


# --- the whole path through _cli_fallback -----------------------------------

class _Sink:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, payload):
        self.sent.append(payload)


def test_a_dying_brain_hands_over_the_checkpoint_not_the_request(monkeypatch):
    """The regression this whole module exists to prevent."""
    handed = {}

    async def fake_cli(out, conv, text, cli, via, note=None, channel="web"):
        handed["text"], handed["via"], handed["note"] = text, via, note

    monkeypatch.setattr(main, "_run_turn_cli", fake_cli)
    monkeypatch.setattr(main.agent_mod, "EXECUTORS", ("copilot", "claude_cli"))
    monkeypatch.setattr(main.agent_mod, "available", lambda n: n == "claude_cli")
    monkeypatch.setattr(main.agent_mod, "runner", lambda n: object())
    resume.save("c1", "fix the login test", "copilot", partial="It's the token refresh.")

    asyncio.run(main._cli_fallback(_Sink(), {"id": "c1"}, "fix the login test",
                                   "copilot", "whatsapp"))
    assert handed["via"] == "claude_cli"
    assert "It's the token refresh" in handed["text"]      # the work carried over
    assert "not starting it over" in handed["text"]
    assert "picking it up" in handed["note"]


def test_the_checkpoint_is_consumed_once_someone_takes_it(monkeypatch):
    """Otherwise a later 'resume' would run the same work a second time."""
    async def fake_cli(*a, **k):
        return None

    monkeypatch.setattr(main, "_run_turn_cli", fake_cli)
    monkeypatch.setattr(main.agent_mod, "EXECUTORS", ("copilot", "claude_cli"))
    monkeypatch.setattr(main.agent_mod, "available", lambda n: n == "claude_cli")
    monkeypatch.setattr(main.agent_mod, "runner", lambda n: object())
    resume.save("c1", "req", "copilot")
    asyncio.run(main._cli_fallback(_Sink(), {"id": "c1"}, "req", "copilot", "web"))
    assert resume.get("c1") is None


def test_with_nothing_available_the_work_is_parked_not_lost(monkeypatch):
    monkeypatch.setattr(main.agent_mod, "EXECUTORS", ("copilot",))
    monkeypatch.setattr(main.agent_mod, "available", lambda n: False)

    def no_api():
        raise RuntimeError("nothing up")

    monkeypatch.setattr(main.agent_mod, "best_model_name", no_api)
    sink = _Sink()
    asyncio.run(main._cli_fallback(sink, {"id": "c1"}, "the real request", "copilot", "whatsapp"))
    said = " ".join(str(p) for p in sink.sent)
    assert "kept the place" in said
    assert resume.get("c1")["request"] == "the real request"   # survives for later
