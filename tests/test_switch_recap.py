"""Brain-switch continuity — the fix for session sprawl's real symptom.

Each CLI brain keeps its own session (formats can't be shared), so switching the
model picker mid-thread used to drop the new brain in blind. A recap bridges it —
but ONLY on a real switch, never after a deliberate 'new chat'.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import copilot_cli, store


@pytest.fixture
def _db(monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db")
    store.init()
    cid = "conv-switch"
    store.add_ui_message(cid, "user", "status of BEP-9397?")
    store.add_ui_message(cid, "assistant", "Fix in Progress, plan MHYYCBMKZTJR")
    store.add_ui_message(cid, "user", "switch to claude and keep going")
    return cid


def test_no_recap_for_a_brand_new_conversation(_db):
    # No other brain has a session → nothing to continue.
    assert copilot_cli._switch_recap({"id": _db}, "Claude Code CLI") == ""


def test_recap_on_a_real_brain_switch(_db):
    store.kv_set(f"copilot_session:{_db}", "sid-abc")   # was on copilot
    recap = copilot_cli._switch_recap({"id": _db}, "Claude Code CLI")
    assert "continuing it after a model switch" in recap
    assert "BEP-9397" in recap
    # the current turn (last user msg) is excluded — it arrives as the prompt
    assert "switch to claude and keep going" not in recap


def test_silent_after_a_new_chat_clears_both(_db):
    # rotate_sessions blanks both; a recap then would resurface cleared context.
    store.kv_set(f"copilot_session:{_db}", "")
    assert copilot_cli._switch_recap({"id": _db}, "Claude Code CLI") == ""


def test_recap_is_bounded(_db):
    for i in range(40):
        store.add_ui_message(_db, "user", f"message number {i} " + "x" * 2000)
    store.kv_set(f"copilot_session:{_db}", "sid")
    recap = copilot_cli._switch_recap({"id": _db}, "Claude Code CLI")
    # at most 6 lines of history + one header line, each truncated
    assert len(recap.splitlines()) <= 7
    assert len(recap) < 4000
