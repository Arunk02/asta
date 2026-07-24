"""Copilot token metering — real input from its own session snapshot.

Copilot exposes no per-message usage (the fields token_audit once read are gone),
but writes a session.shutdown with currentTokens = the context it carried, which
is the turn's input. These lock in that we read it, and fall back cleanly when it
isn't there rather than inventing a measured number.
"""

from __future__ import annotations

import json

from app import copilot_cli, store


def _fake_session(tmp_path, monkeypatch, sid, *events):
    monkeypatch.setattr(copilot_cli, "COPILOT_SESSIONS", tmp_path)
    d = tmp_path / sid
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))


def test_real_input_comes_from_the_session_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()
    store.kv_set("copilot_session:c1", "sid-1")
    _fake_session(tmp_path, monkeypatch, "sid-1",
                  {"type": "assistant.turn_start", "data": {}},
                  {"type": "session.shutdown",
                   "data": {"currentTokens": 24654, "toolDefinitionsTokens": 14498}})
    u = copilot_cli.last_turn_usage({"id": "c1"}, reply_chars=800)
    assert u.measured is True
    assert u.input == 24654
    assert u.output == 200            # 800 chars / 4
    assert u.effective > 24000        # dominated by the real input


def test_no_snapshot_yet_returns_unmeasured(tmp_path, monkeypatch):
    """Better an honest estimate downstream than a fake measured zero."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()
    store.kv_set("copilot_session:c2", "sid-2")
    _fake_session(tmp_path, monkeypatch, "sid-2",
                  {"type": "assistant.turn_start", "data": {}})  # no shutdown
    u = copilot_cli.last_turn_usage({"id": "c2"}, reply_chars=800)
    assert u.measured is False and u.input == 0


def test_unknown_conversation_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()
    monkeypatch.setattr(copilot_cli, "COPILOT_SESSIONS", tmp_path)
    u = copilot_cli.last_turn_usage({"id": "never-seen"}, reply_chars=10)
    assert u.measured is False and u.total == 0
