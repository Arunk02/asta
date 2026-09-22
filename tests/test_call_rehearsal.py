"""The rehearsal's judge: turning what the simulated colleague heard into a verdict.

The rehearsal itself needs Chrome, the voice server and a brain, so it runs by
hand (`python -m app.call_rehearsal`). What it CONCLUDES is plain arithmetic
over timestamps, and a wrong conclusion would pass a call that was not good
enough — so that part is held here.
"""

from __future__ import annotations

from app import call_rehearsal as R


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
