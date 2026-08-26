"""Text cut for a phone must still read as a sentence.

Arun, 26 Aug, on two messages that stopped dead: *"message getting truncked in
btw no proper conclusion"*. He was looking at

    • telikos-booking-service: could not create a worktree: fatal: … is already
      checked out at '…/task-69/telikos-booking-service'
    Preparing worktree (c

The cause was hard character slices — `msg[:160]`, `text[:200]` — in the places
that prepare something for him to read. Each was right that a bound was needed
and wrong about where to put it: a cut mid-word reads as a crash, and nothing
tells him whether the missing part mattered.
"""

from __future__ import annotations

import pytest

from app import clip

_GIT_FAILURE = (
    "Preparing worktree (checking out 'feature/TELIKOS-123')\n"
    "fatal: 'feature/TELIKOS-123' is already checked out at "
    "'/Users/arun.k.k/booking-workspace/.asta-worktrees/task-69/telikos-booking-service'\n")


def test_short_text_is_untouched():
    assert clip.clip("all done.", 200) == "all done."


def test_a_cut_never_lands_mid_word():
    out = clip.clip("the quick brown fox jumps over the lazy dog " * 5, 60)
    assert len(out) <= 60
    assert out.endswith("…")
    assert not out.rstrip("… ").endswith(("qu", "brow", "jum"))
    assert out.rstrip("… ").split()[-1] in {"the", "quick", "brown", "fox", "jumps",
                                            "over", "lazy", "dog"}


def test_a_cut_says_it_cut():
    """Otherwise a trimmed message is indistinguishable from a crashed one."""
    assert clip.clip("x " * 200, 40).endswith("…")


def test_a_line_break_is_preferred_to_a_word_break():
    out = clip.clip("first line is quite long here\nsecond line runs on and on", 40)
    assert out == "first line is quite long here…"


def test_one_enormous_token_still_yields_something():
    """No boundary exists; showing nothing would be worse than a hard cut."""
    out = clip.clip("A" * 500, 50)
    assert 0 < len(out) <= 50 and out.endswith("…")


def test_a_boundary_too_early_is_ignored():
    """A break at character three is technically a word boundary and would throw
    away everything he asked for."""
    out = clip.clip("hi " + "B" * 300, 60)
    assert len(out) > 30


def test_the_reason_survives_not_the_narration():
    """git prints its progress first and fails last, so a leading slice keeps the
    progress and cuts the reason off the end — which is exactly what he saw."""
    out = clip.problem(_GIT_FAILURE)
    assert out.startswith("fatal:")
    assert "already checked out" in out
    assert "Preparing worktree" not in out


def test_output_with_no_named_error_falls_back_to_the_last_line():
    out = clip.problem("step one\nstep two\nit did not work")
    assert out == "it did not work"


@pytest.mark.parametrize("head", ["fatal: boom", "error: boom", "ERROR boom",
                                  "Exception: boom", "Caused by: boom"])
def test_the_usual_ways_a_tool_names_a_failure(head):
    assert clip.problem(f"progress line\n{head}\ntrailing noise").startswith(head.split(":")[0])


def test_empty_output_is_empty_not_a_crash():
    assert clip.problem("") == "" and clip.problem("   \n  ") == ""


def test_the_worktree_note_uses_it():
    """The site that produced the message he complained about."""
    from pathlib import Path
    src = Path("app/worktrees.py").read_text()
    assert "[:160]" not in src, "a hard slice is back in the worktree note"
    assert "clip_mod.problem(msg)" in src


def test_the_ledger_summary_uses_it():
    """`what` is rendered back to him in chases and in "what's on my plate"."""
    from pathlib import Path
    src = Path("app/notify.py").read_text()
    assert "text[:200]" not in src
    assert "clip_mod.clip(text, 200)" in src
