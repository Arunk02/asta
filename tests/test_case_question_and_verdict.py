"""A case is answered as asked, with one proven cause (17 Nov, a colleague's two cases).

VTS: A colleague asked why a booking went to TMS while VTS said pending. The turns
that did the work held only Arun's "check on the request he asked" and answered
whether the booking completed. EH: four answers, each a confident guess. Both
right answers were one payload line and one `if` away.

Synthetic ids and names only.
"""
from __future__ import annotations

import time

import pytest

from app import agent, answers, booking_case, capabilities, copilot_cli, resume, store, verdict

WRAPPED = ("Arun did NOT approve sending the draft as-is. His feedback:\n"
           "sit booking number it is check on the request he asked\n\n"
           "Revise accordingly — change only what he asked for, in his words, and add nothing "
           "from his other conversations. When it's ready to send again, stage it with "
           "prepare_to_send.")


def test_his_feedback_not_the_wrapper_is_what_a_turn_is_about():
    assert resume.ranking_text(WRAPPED) == "sit booking number it is check on the request he asked"


def _seed_case(conv_id, who="Dev Colleague"):
    store.add_ui_message(conv_id, "assistant",
                         f"*{who}* wants you to check VTS for the booking.", {"via": "answer"})
    now = time.time()
    store.save_teams_messages([
        {"key": "k1", "chat": who, "sender": who, "sent_at": now - 60,
         "text": "Booking UI https://ui.example.test/booking/MXAAAA1111BB"},
        {"key": "k2", "chat": who, "sender": who, "sent_at": now - 50,
         "text": "for this booking, can you see from VTS? status is pending, but still it's processed"},
    ])


def test_the_colleagues_own_words_are_the_question(monkeypatch):
    conv = store.create_conversation(model="claude_cli", workspace=None)
    _seed_case(conv["id"])
    asked = copilot_cli.colleague_ask(conv["id"])
    assert asked.startswith("Dev Colleague: ")
    assert "status is pending, but still it's processed" in asked
    assert "MXAAAA1111BB" in asked


def test_the_recipe_carries_their_question_and_the_one_cause_shape():
    out = booking_case.for_turn("check in sit", "",
                                question="Dev: why is MXAAAA1111BB processed while VTS is pending?")
    assert "THEIR QUESTION — answer exactly this" in out
    assert "why is MXAAAA1111BB processed while VTS is pending?" in out
    assert "Confidence: CONFIRMED" in out and "Never two causes" in out
    assert "find the code that decides it" in out.lower()      # a WHY question
    assert "code_history" in out


@pytest.mark.parametrize("analysis", [
    # The five wrong answers of 17 Nov, reworded: each a cause with no proof.
    "VTS showing pending is just a display lag on their side, not stuck on our end.",
    "There's no gating on that PENDING status, which is why it went to TMS.",
    "The root cause is that a doc-only amend request doesn't carry the full reference list.",
    "It is either the AP amend flow or the EH mapper, most likely the mapper.",
])
def test_a_cause_without_proof_is_not_an_answer(analysis):
    assert verdict.problems(analysis)
    assert not verdict.confirmed(analysis)
    assert answers.unconfirmed(analysis)


def test_a_proven_cause_passes():
    good = ("Question: why did the ocean EH entry fire on a doc-only save?\n"
            "Why: OceanCarrierImportsAmendEventHistoryMapper.java:72 fires on ANY reference "
            "that differs, and VESSEL_ATA was new in the request.\n"
            "Timeline: 10:11:12 booking-service EventDispatcher published "
            "'Ocean transportation details (Imports)' UPDATED with every field unchanged.\n"
            "Not confirmed: nothing.\nNext step: diff only the displayed references.\n"
            "Confidence: CONFIRMED")
    assert verdict.confirmed(good)
    assert answers.unconfirmed(good) == ""


def test_a_cause_he_did_not_ask_to_send_is_held(monkeypatch):
    monkeypatch.setattr(capabilities, "said_this_turn",
                        lambda: "that it wont skip EH, check the mapper, did you understand or not ?")
    held = agent._unasked_cause("issue is the doc-only amend request doesn't carry the refs")
    assert held.startswith("Not sent")


def test_his_send_is_his_yes(monkeypatch):
    monkeypatch.setattr(capabilities, "said_this_turn", lambda: "send the analysis to dana")
    assert agent._unasked_cause("root cause: the mapper diffs every reference") == ""


def test_plain_back_and_forth_is_never_held(monkeypatch):
    monkeypatch.setattr(capabilities, "said_this_turn", lambda: "ok")
    assert agent._unasked_cause("PR is up bro, will share the build once merged") == ""


def test_a_link_cards_address_is_kept_in_the_page_script():
    import inspect
    from app import teams_bridge
    src = inspect.getsource(teams_bridge)
    assert "A link CARD shows a page title" in src and "a.getAttribute('href')" in src
