"""Triage — what is this, and does it really need Arun?

Two reported bugs are pinned here:
  1. Already-opened messages kept re-notifying, because the dedup key was a
     rendered string containing a relative timestamp — it changed by itself.
  2. Every mail from a human was pushed as if it needed him, preview and all,
     so someone's random thought interrupted exactly like a blocker.
"""

from __future__ import annotations

import asyncio

from app import triage


# --- stable identity (bug 1) ------------------------------------------------

def test_the_same_message_keys_the_same_as_time_passes():
    """THE re-notification bug: the feed re-renders the age, so the old key
    ('first 150 chars') drifted and the message looked new on every poll."""
    a = triage.stable_key("Priya — mentioned you in Release — can you review? — 2m")
    b = triage.stable_key("Priya — mentioned you in Release — can you review? — 1h")
    c = triage.stable_key("Priya — mentioned you in Release — can you review? — Yesterday")
    assert a == b == c


def test_clock_times_and_dates_do_not_change_the_key():
    assert (triage.stable_key("Bob — standup notes — 10:30 AM")
            == triage.stable_key("Bob — standup notes — 4:05 pm"))
    assert (triage.stable_key("Bob — standup notes — 12/03")
            == triage.stable_key("Bob — standup notes — 14/07/2026"))


def test_unread_badge_does_not_change_the_key():
    """Reading it must not make it look like a different item."""
    assert (triage.stable_key("Unread: Priya — deploy is red")
            == triage.stable_key("Priya — deploy is red"))


def test_different_messages_still_key_differently():
    """The key must not be so aggressive that real news is swallowed as a dupe."""
    assert (triage.stable_key("Priya — can you review the PR?")
            != triage.stable_key("Priya — the build is broken"))


def test_key_is_bounded():
    assert len(triage.stable_key("x " * 500)) <= 160


# --- verdicts (bug 2) -------------------------------------------------------

def test_a_random_thought_is_fyi_and_asks_nothing():
    """Arun's exact complaint: 'some random thoughts also getting notifications'."""
    v = triage.classify("Sam", "Just a thought on the caching idea",
                        "Was thinking we could pre-warm the cache someday.")
    assert v.action is False
    assert v.render().startswith("·")            # no red dot, no demand


def test_a_real_ask_needs_him():
    v = triage.classify("Priya", "Can you review PR #12 today?")
    assert v.action is True
    assert "Priya" in v.one_line and v.render().startswith("🔴")


def test_approval_requests_need_him():
    for subj in ("Please approve the release", "Awaiting your sign-off",
                 "Action required: timesheet", "Need your input on the design"):
        assert triage.classify("X", subj).action is True, subj


def test_explicit_fyi_never_asks():
    for subj in ("FYI — deploy finished", "Sharing the meeting notes",
                 "No action needed: policy update", "Automated report"):
        assert triage.classify("X", subj).action is False, subj


def test_addressed_but_no_ask_is_told_once_quietly():
    """A DM that isn't a request should be known, not interrupting."""
    v = triage.classify("Ravi", "mentioned you in General", "nice work on the launch")
    assert v.action is False
    assert v.why == "addressed to you, no ask"


def test_a_bare_question_in_a_dm_still_counts_as_addressed():
    v = triage.classify("Ravi", "replied to you", "any update?")
    assert v.action is True


def test_one_line_is_actually_one_line_and_short():
    """The old format appended a 180-char preview per mail — a wall on a phone."""
    v = triage.classify("Sam", "Quarterly planning", "x" * 900)
    assert "\n" not in v.one_line
    assert len(v.one_line) < 240


def test_missing_fields_never_crash():
    v = triage.classify("", "", "")
    assert v.one_line and v.action is False


# --- batching ---------------------------------------------------------------

def test_summary_puts_asks_first_and_labels_the_fyi_tail():
    vs = [triage.classify("Priya", "Can you approve the release?"),
          triage.classify("Sam", "FYI — nightly finished"),
          triage.classify("Lee", "Sharing my notes")]
    text, needs = summarize_ok(vs)
    assert needs is True
    assert text.index("needs you") < text.index("FYI")
    assert "nothing needed from you" in text


def test_an_all_fyi_batch_does_not_claim_to_need_him():
    vs = [triage.classify("Sam", "FYI — nightly finished")]
    text, needs = summarize_ok(vs)
    assert needs is False
    assert "needs you" not in text


def summarize_ok(vs):
    return triage.summarize(vs, "Outlook")


# --- the local-model tie-breaker is free, optional, and one-way -------------

def test_refine_upgrades_an_ambiguous_item_when_the_local_model_says_act(monkeypatch):
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "ACT")
    v = triage.classify("Sam", "quick one about the invoice")
    assert v.action is False
    out = asyncio.run(triage.refine(v, "Sam", "quick one about the invoice"))
    assert out.action is True


def test_refine_never_downgrades_a_clear_ask(monkeypatch):
    """A rule-confirmed ask must not be talked out of it by a small model."""
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "FYI")
    v = triage.classify("Priya", "Can you approve the release?")
    out = asyncio.run(triage.refine(v, "Priya", "Can you approve the release?"))
    assert out.action is True


def test_refine_falls_back_to_the_rules_when_the_local_model_is_down(monkeypatch):
    from app import memory

    def boom(*a, **k):
        raise RuntimeError("LM Studio not running")

    monkeypatch.setattr(memory, "local_llm_complete", boom)
    v = triage.classify("Sam", "thoughts on the roadmap")
    out = asyncio.run(triage.refine(v, "Sam", "thoughts on the roadmap"))
    assert out == v                                # unchanged, never crashes


def test_refine_handles_an_async_local_model(monkeypatch):
    """local_llm_complete is sync for some backends and async for others."""
    from app import memory

    async def acomplete(*a, **k):
        return "ACT"

    monkeypatch.setattr(memory, "local_llm_complete", acomplete)
    v = triage.classify("Sam", "about the invoice")
    assert asyncio.run(triage.refine(v, "Sam", "about the invoice")).action is True


# --- quoting the message, not padding around it ------------------------------
#
# Arun: "summarise the exact line based on message in teams and outlook which u
# got.. dont send unwanted related details i feel". The old version appended the
# FIRST sentence of the preview, which in a real mail is a greeting — so every
# notification carried a line of "Hi Arun, hope you're doing well" before the
# part that mattered.

def test_the_quoted_line_is_the_one_that_asks():
    v = triage.classify(
        "Priya", "Migration plan",
        "Hi Arun, hope you're doing well. The schema draft is attached. "
        "Could you review it before Thursday?")
    assert "Could you review it before Thursday?" in v.one_line
    assert "hope you" not in v.one_line.lower()


def test_the_greeting_alone_is_never_what_gets_quoted():
    v = triage.classify("Sam", "Release checklist",
                        "Hello Arun! Please approve the release notes.")
    assert v.one_line.count("Hello") == 0
    assert "approve the release notes" in v.one_line


def test_nothing_is_appended_when_the_subject_already_asked():
    """The subject said it. Repeating the body underneath is the padding he asked
    to stop seeing."""
    v = triage.classify("Sam", "Please approve the release notes",
                        "Details are in the doc, everything is ready to go.")
    assert v.one_line == "Sam: Please approve the release notes"


def test_an_fyi_is_the_subject_and_nothing_else():
    v = triage.classify("Newsletter", "Weekly engineering digest",
                        "Lots and lots of things happened this week, here they are.")
    assert v.one_line == "Newsletter: Weekly engineering digest"
    assert v.action is False


def test_a_body_with_no_ask_adds_no_quote():
    v = triage.classify("Sam", "Can you take a look?",
                        "It's the third file down. No rush at all.")
    assert "third file down" not in v.one_line


def test_a_long_quoted_line_is_trimmed_on_a_word_boundary():
    ask = "Could you please review " + "the extremely detailed migration document " * 5
    v = triage.classify("Priya", "Migration", ask)
    quoted = v.one_line.split("—", 1)[1] if "—" in v.one_line else ""
    assert len(quoted) < 140
    assert quoted.strip().endswith("…”") or quoted.strip().endswith("”")


def test_the_quote_is_marked_as_a_quote():
    """He is reading someone else's words. Presenting them unmarked, mixed with
    Asta's own summary line, blurs who said what."""
    v = triage.classify("Priya", "Migration", "Can you review the schema?")
    assert "“" in v.one_line and "”" in v.one_line
