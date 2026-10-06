"""The rehearsal's judge: turning what the simulated colleague heard into a verdict.

The rehearsal itself needs Chrome, the voice server and a brain, so it runs by
hand (`python -m app.call_rehearsal`) or as an opt-in integration test. What it
CONCLUDES is plain arithmetic over timestamps, and a wrong conclusion would
pass a call that was not good enough — so that part is held here.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app import call_rehearsal as R, store


def _sc(**expect):
    return R.Scenario("t", "t", [("connected", {"say": "Hello?"}), ("asta_done", {"say": "Yes."})],
                      expect=expect)


def _span(start, end):
    return {"start": start, "end": end}


def test_an_mm_hm_is_first_sound_and_the_reply_is_what_follows():
    timeline = [{"event": "connected", "at": 0},
                {"event": "colleague", "text": "Hello?", "start": 300, "end": 800},
                {"event": "colleague", "text": "Yes.", "start": 9000, "end": 9400}]
    spans = [_span(1500, 6000),            # the greeting
             _span(9900, 10250),           # "mm-hm", 0.5 s after "Yes."
             _span(11200, 13000)]          # the reply, 1.8 s after
    r = R._judge(_sc(reply_within=3.0, sound_within=1.2), timeline, spans, "Talked to", 20)
    assert r["first_sound"][1] == 0.5 and r["reply_gaps"][1] == 1.8
    assert r["passed"], r["fails"]


def test_a_slow_reply_fails_even_when_the_mm_hm_was_quick():
    timeline = [{"event": "connected", "at": 0},
                {"event": "colleague", "text": "Hello?", "start": 300, "end": 800},
                {"event": "colleague", "text": "Yes.", "start": 9000, "end": 9400}]
    spans = [_span(1500, 6000), _span(9900, 10250), _span(14400, 16000)]
    r = R._judge(_sc(reply_within=3.0, sound_within=1.2), timeline, spans, "Talked to", 20)
    assert not r["passed"] and r["reply_gaps"][1] == 5.0


def test_a_fast_reply_does_not_pass_if_asta_missed_the_interruption():
    timeline = [{"event": "connected", "at": 0},
                {"event": "colleague", "text": "Sorry, wait, who is this?", "start": 1000, "end": 2300}]
    spans = [_span(200, 2200), _span(2800, 4500)]
    r = R._judge(_sc(stops_within=1.5, must_hear="who is this"), timeline, spans,
                 "Talked to Riya Test", 10, heard_lines=["Okay, got it. Bye."])
    assert not r["passed"] and "never heard the interruption" in r["fails"][0]


def test_an_audio_check_does_not_pass_if_asta_brings_up_old_work():
    r = R._judge(_sc(audio_only=True), [{"event": "connected", "at": 0}],
                 [_span(100, 1800)], "Talked to Riya Test", 3,
                 saying=[(100, "Any update on booking PR 1409?")])
    assert "audio-only rehearsal drifted into Arun's work" in r["fails"]


def test_speaking_into_a_voicemail_fails():
    timeline = [{"event": "connected", "at": 0}]
    r = R._judge(_sc(asta_silent=True), timeline, [_span(2000, 5000)], "went to voicemail", 10)
    assert not r["passed"]


def test_a_listeners_mm_hm_in_a_thinking_pause_is_not_a_cut_in():
    timeline = [{"event": "connected", "at": 0},
                {"event": "colleague", "text": "Hello?", "start": 300, "end": 800},
                {"event": "paused", "at": 9000},
                {"event": "colleague", "text": "So what I think is … we should wait.",
                 "start": 8000, "end": 11500}]
    spans = [_span(1500, 6000), _span(9500, 9850), _span(12500, 14000)]
    r = R._judge(_sc(no_cut_in=True, reply_within=3.5), timeline, spans, "Talked to", 20)
    assert "cut in while they paused to think" not in r["fails"]


def test_a_failing_rehearsal_is_reported_by_health_and_a_passing_one_is_not():
    import json
    import time
    path = R._latest_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": time.time(), "results": [
        {"scenario": "quick-yes", "passed": True}, {"scenario": "interrupts", "passed": False}]}))
    assert R.latest_failures() == "interrupts (1/2)"
    path.write_text(json.dumps({"at": time.time(), "results": [
        {"scenario": "quick-yes", "passed": True}]}))
    assert R.latest_failures() == ""


def test_a_rehearsal_from_days_ago_does_not_speak_for_today():
    import json
    import time
    path = R._latest_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": time.time() - 5 * 86400, "results": [
        {"scenario": "interrupts", "passed": False}]}))
    assert R.latest_failures() == ""


@pytest.mark.skipif(os.environ.get("ASTA_SELF_TALK_TEST") != "1",
                    reason="uses two local browser endpoints, Voicebox and a real call brain")
def test_asta_to_asta_call_hears_and_answers_an_interruption(tmp_path, monkeypatch):
    from app import voice

    if not (Path(__file__).resolve().parents[1] / ".env").is_file():
        pytest.skip("real call brain needs a configured local Asta checkout")
    monkeypatch.setattr(voice, "BASE", voice.CONFIGURED_BASE)
    monkeypatch.setattr(voice, "DEFAULT_PROFILE", "Asta (male)")
    original_db = store.DB_PATH
    try:
        scenario = next(s for s in R.SCENARIOS if s.name == "interrupts")
        report = asyncio.run(R.run(scenario, tmp_path))
    finally:
        store.DB_PATH = original_db
    assert report["passed"], report["fails"]
    assert report["reply_gaps"] and all(g is not None for g in report["reply_gaps"])
    print(f"Asta-to-Asta call: heard interruption; reply gaps {report['reply_gaps']}s")
