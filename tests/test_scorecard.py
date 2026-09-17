"""The daily scorecard reads what happened and judges it against the plan's targets.

It must never count Asta talking to itself as Arun, never show "good" for a
metric with no data, and keep a daily history the 500-row traces table cannot.
"""

from __future__ import annotations

import datetime as dt
import json
import time

from app import scorecard, store


def _row(card, key):
    return next(r for r in card["rows"] if r["key"] == key)


def test_an_empty_system_is_judged_na_not_good():
    card = scorecard.compute()
    assert _row(card, "reply_p50")["state"] == "na"
    assert _row(card, "reply_length")["state"] == "na"


def test_replies_are_measured_on_phone_channels_with_first_word():
    for total, first in ((60_000, 48_000), (20_000, 5_000), (30_000, 9_000)):
        store.add_trace("c", "claude_cli", "whatsapp", first, total, 2, 10, 100_000, 0, 5, [],
                        context_tokens=120_000)
    card = scorecard.compute()
    row = _row(card, "reply_p50")
    assert row["value"] == 30 and row["state"] == "warn"
    assert "first word 9 s" in row["note"]
    assert _row(card, "context_per_call")["state"] == "warn"


def test_his_words_exclude_what_asta_wrote_itself():
    assert scorecard.his_words("Continue now — do not wait for me. The next step…") == ""
    assert scorecard.his_words("Context: IT Service Desk: Incident …") == ""
    assert scorecard.his_words(
        "Arun did NOT approve sending the draft as-is. His feedback: why is it stuck again "
        "Revise accordingly. When it's ready…") == "why is it stuck again"
    assert scorecard.his_words("why u cant able to create branch") == "why u cant able to create branch"


def test_corrections_count_only_his_frustration():
    for text in ("why u cant able to create branch and push , what is blocking ?",
                 "Did you fixed the issue in release branch or not yet",
                 "Continue now — do not wait for me. why is it slow",
                 "Retrigger again",
                 "thanks, looks good"):
        store.add_ui_message("c", "user", text, {"channel": "whatsapp"})
    c = scorecard.corrections(time.time() - 3600, time.time() + 1)
    assert c == {"messages": 4, "corrections": 2}


def test_pushes_duplicates_and_the_task_that_pushed_most():
    for text in ("✅ DONE — #94 thing", "✅ DONE — #94 thing", "📋 *PLAN #94*", "🔴 #7 red"):
        store.add_notification(text, "task")
    p = scorecard.pushes(time.time() - 86400, time.time() + 1)
    assert p["total"] == 4 and p["most_for_one_task"] == 3
    assert p["duplicate_share"] == 0.25


def test_a_limit_that_failed_a_task_is_counted_and_judged_bad():
    t = store.create_task("x", "analysis", "p", None)
    store.update_task(t["id"], status="failed",
                      error="claude exited 1: You've hit your session limit · resets 5pm")
    card = scorecard.compute()
    row = _row(card, "limit_failures")
    assert row["value"] == 1 and row["state"] == "bad"


def test_a_plan_left_as_done_is_caught():
    t = store.create_task("x", "code", "p", None)
    store.update_task(t["id"], status="done", result="STRUCTURE\n  A  adds x\n\nPLAN READY")
    assert _row(scorecard.compute(), "false_done")["value"] == 1


def test_snapshots_build_a_history_the_traces_table_cannot():
    day = dt.date.today() - dt.timedelta(days=1)
    scorecard.snapshot(day)
    hist = scorecard.history(3)
    assert hist and hist[-1]["date"] == day.isoformat()
    assert "pushes_per_day" in hist[-1]["values"]


def test_backfill_only_fills_missing_days():
    made = scorecard.backfill(3)
    assert made == 3
    assert scorecard.backfill(3) == 0


def test_workworld_results_appear_as_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(scorecard, "WORKWORLD_LAST", tmp_path / "absent.json")
    store.kv_set("workworld:last", json.dumps({"at": "2026-09-12 01:10", "sets": {
        "incidents": {"total": 10, "passed": 9}, "constitution": {"total": 5, "passed": 4}}}))
    card = scorecard.compute()
    assert _row(card, "ww_incidents")["state"] == "good"
    assert _row(card, "ww_constitution")["state"] != "good"   # constitution means 100%


def test_the_page_and_endpoint_exist():
    from pathlib import Path
    src = Path("app/main.py").read_text()
    assert '@app.get("/api/scorecard"' in src and '@app.get("/scorecard")' in src
    assert Path("ui/scorecard.html").exists()
    assert 'daemon.start("scorecard", scorecard.loop)' in src
