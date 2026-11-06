"""The two-week journal records the day and reviews where Asta went wrong.

Synthetic day: one correction from him, one draft he rewrote, a false send
claim, a failed task and a turn timing. Names and text are made up.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import time

from app import journal, store


def _seed():
    conv = store.create_conversation("claude_cli", None)
    store.add_ui_message(conv["id"], "assistant", "Sent the fix to Pat.", {})
    store.add_ui_message(conv["id"], "user", "why did you send it to the wrong repo", {})
    store.add_ui_message(conv["id"], "user", "Arun did NOT approve sending the draft as-is. His feedback:\n"
                         "make it shorter\n\nRevise accordingly — change only what he asked for", {})
    store.record_outcome("turn", "false_send_claim", subject=conv["id"], detail="Sent to Pat")
    t = store.create_task("Fix the date mapping", "code", "p", "ws")
    store.update_task(t["id"], status="failed", error="checkout missing")
    store.add_trace(conv["id"], "claude_cli", "whatsapp", 1200, 30_000, 10, 5, 0, 0, 10, ["x"])


def test_a_day_is_recorded_to_a_local_file_and_reviewed(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    _seed()
    p = journal.record()
    assert p.exists() and p.parent == tmp_path / "journal"
    with gzip.open(p, "rt") as f:
        day = json.load(f)
    assert day["tasks"] and day["his_messages"] and day["traces"]
    text = journal.review(1)
    assert "why did you send it to the wrong repo" in text and "after Asta: “Sent the fix to Pat.”" in text
    assert "his change: “make it shorter”" in text
    assert "claimed a send that did not happen" in text
    assert "failed: Fix the date mapping" in text and "checkout missing" in text
    assert "chat turns: 1" in text


def test_files_older_than_the_window_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setenv("ASTA_JOURNAL_DAYS", "21")
    (tmp_path / "journal").mkdir()
    old = journal.path_for(dt.date.today() - dt.timedelta(days=30))
    keep = journal.path_for(dt.date.today() - dt.timedelta(days=5))
    for p in (old, keep):
        with gzip.open(p, "wt") as f:
            f.write("{}")
    journal.prune()
    assert not old.exists() and keep.exists()


def test_no_journal_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    assert "No journal yet" in journal.review(14)


def test_the_journal_lives_under_gitignored_data():
    assert journal.JOURNAL_DIR.parts[-2:] == ("data", "journal")
    ignored = (store.ROOT / ".gitignore").read_text()
    assert "data/" in ignored.splitlines()
