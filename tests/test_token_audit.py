"""The waste classifier had no tests — and it's exactly the code that must be right:
a wrong estimate sends the evolution loop to fix the wrong thing. Tester's lens —
detect_waste is a pure function of a normalized record, so every category gets a
record that trips it and a lean record that trips none.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app import token_audit as ta


def _rec(**over) -> dict:
    r = ta._norm()
    r["executor"] = "claude"
    r.update(over)
    return r


# --- detect_waste: one record per category ----------------------------------

def test_lean_run_flags_nothing():
    r = _rec(calls=6, out_tokens=2000, new_tokens=8000,
             reads=[("/w/A.java", True)], bash=["ls"])
    assert ta.detect_waste(r) == {}


def test_duplicate_reads():
    r = _rec(calls=4, reads=[("/w/A.java", True), ("/w/A.java", True), ("/w/B.java", True)])
    w = ta.detect_waste(r)
    assert w["duplicate_reads"]["count"] == 1          # A opened twice = 1 redundant
    assert w["duplicate_reads"]["est_tokens"] == 800


def test_full_reads_are_the_unbounded_ones():
    r = _rec(calls=3, reads=[("/w/A.java", False), ("/w/B.java", True), ("/w/C.java", False)])
    w = ta.detect_waste(r)
    assert w["full_reads"]["count"] == 2
    assert "duplicate_reads" not in w


def test_fat_outputs_are_cache_amplified():
    # A fat result early in the run is re-cached on every later turn, so its cost
    # must exceed the raw excess.
    r = _rec(calls=20, results={"t1": 8000}, result_turn={"t1": 1})
    w = ta.detect_waste(r)
    raw_excess = (8000 - ta.FAT_OUTPUT_CHARS) // ta.CHARS_PER_TOKEN
    assert w["fat_outputs"]["count"] == 1
    assert w["fat_outputs"]["est_tokens"] > raw_excess   # amplification applied


def test_fat_output_amplification_is_capped():
    early = ta.detect_waste(_rec(calls=200, results={"t": 8000}, result_turn={"t": 1}))
    same = ta.detect_waste(_rec(calls=45, results={"t": 8000}, result_turn={"t": 1}))
    # remaining turns are capped at 40, so 200-call and 45-call runs cost the same.
    assert early["fat_outputs"]["est_tokens"] == same["fat_outputs"]["est_tokens"]


def test_excess_greps_only_over_threshold():
    assert "excess_greps" not in ta.detect_waste(_rec(bash=["grep x"] * 8))
    w = ta.detect_waste(_rec(bash=["grep x"] * 11))
    assert w["excess_greps"]["count"] == 11
    assert w["excess_greps"]["est_tokens"] == 3 * 600    # 3 over the floor of 8


def test_narration_bloat():
    assert "narration" not in ta.detect_waste(_rec(text_blocks=[2000]))         # ~500 tok
    w = ta.detect_waste(_rec(text_blocks=[4000, 4000]))                          # ~2000 tok
    assert w["narration"]["est_tokens"] == 2000


def test_replan_recache_signature():
    # cache-write ≫ output is the resume/re-plan fingerprint.
    assert "replan_recache" in ta.detect_waste(_rec(new_tokens=100000, out_tokens=500))
    assert "replan_recache" not in ta.detect_waste(_rec(new_tokens=4000, out_tokens=1000))


# --- grading + trend --------------------------------------------------------

def test_grade_bands():
    assert ta._grade(0.02).startswith("A")
    assert ta._grade(0.08).startswith("B")
    assert ta._grade(0.18).startswith("C")
    assert ta._grade(0.40).startswith("D")


def test_trend_direction():
    assert "baseline" in ta._trend(None, 0.2)
    assert ta._trend({"waste_ratio": 0.30}, 0.20).startswith("↓")   # improved
    assert ta._trend({"waste_ratio": 0.10}, 0.20).startswith("↑")   # worse
    assert "flat" in ta._trend({"waste_ratio": 0.201}, 0.200)


# --- parser end to end ------------------------------------------------------

def test_audit_session_parses_a_claude_log():
    log = [
        {"type": "assistant", "message": {
            "usage": {"output_tokens": 50, "input_tokens": 100,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 20},
            "content": [{"type": "tool_use", "name": "Read",
                         "input": {"file_path": "/w/A.java"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 9000}]}},
    ]
    p = Path(tempfile.mkdtemp()) / "sess.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in log))
    rep = ta.audit_session(p)
    assert rep["executor"] == "claude"
    assert rep["calls"] == 1
    assert "full_reads" in rep["waste"]        # /w/A.java read with no line bound
    assert "fat_outputs" in rep["waste"]       # 9k-char tool result
    assert rep["grade"][0] in "ABCD"


# --- aggregate / capability guardrails --------------------------------------

def test_audit_recent_handles_empty_window(monkeypatch):
    monkeypatch.setattr(ta, "recent_sessions", lambda *a, **k: [])
    assert ta.audit_recent()["sessions"] == []


def test_capability_message_when_no_sessions(monkeypatch):
    from app import agent
    monkeypatch.setattr(ta, "recent_sessions", lambda *a, **k: [])
    out = agent.token_audit(12)
    assert "No worker sessions" in out and "12h" in out
