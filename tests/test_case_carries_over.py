"""One person's case is one case: handed over once, and built on (5 Nov).

Synthetic replay: a colleague asked why a date was missing; Arun approved a reply
on it. Their next two follow-ups were briefed cold — no earlier finding, nothing
of what they had been told — and stopped on his "do not investigate incidents
unless I hand one over" rule. Names, ids and wording are made up.
"""
from __future__ import annotations

import time

from app import answers, responder, store

CHAT = "Pat Lee"


def _done(title, result, *, chat=CHAT, prompt="", at=None):
    t = store.create_task(title, "analysis", prompt or title, None, teams_chat=chat)
    store.update_task(t["id"], status="done", result=result)
    if at is not None:
        import sqlite3
        with sqlite3.connect(store.DB_PATH) as c:
            c.execute("UPDATE tasks SET created_at=? WHERE id=?", (at, t["id"]))
    return t["id"]


def test_approving_a_reply_hands_their_case_over():
    assert responder.handed_rider(CHAT) == ""
    answers.sent({"to": CHAT, "what": "checked it, the date is there"})
    rider = responder.handed_rider(CHAT)
    assert "handed this person's case over" in rider and "do not stop" in rider


def test_the_hand_over_ends_and_a_group_send_is_not_one():
    answers.sent({"to": "Team room", "what": "x", "to_group": True})
    assert responder.handed_rider("Team room") == ""
    responder.note_handed(CHAT, now=time.time() - responder.CASE_HANDED_SECONDS - 60)
    assert responder.handed_rider(CHAT) == ""


def test_a_follow_up_brief_carries_earlier_findings_and_what_was_said():
    _done("Pat asked: why is the date missing",
          "ANALYSIS:\nThe tracker answered TRACKING with no dates.\n\nREPLY:\nno dates yet")
    _done("Where is the date dropped", "ANALYSIS:\nThe mapper copies every date.\n\nREPLY:\n-",
          chat="", prompt="Pat Lee asked where the date is dropped")
    _done("Old case", "ANALYSIS:\nunrelated\n\nREPLY:\n-",
          at=time.time() - responder.CASE_WINDOW_SECONDS - 60)
    now = time.time()
    store.save_teams_messages([
        {"key": "k1", "chat": CHAT, "sender": CHAT, "text": "why is the date missing",
         "sent_at": now - 600, "stamp": "19:39"},
        {"key": "k2", "chat": CHAT, "sender": "Arunkumar K", "text": "it is dropped in svc-beta",
         "sent_at": now - 300, "stamp": "20:07"}])
    block = responder.case_so_far(CHAT, CHAT)
    assert "TRACKING with no dates" in block and "copies every date" in block
    assert "it is dropped in svc-beta" in block, "what they were told, so it can be corrected"
    assert "why is the date missing" not in block.split("Already said")[1]
    assert "unrelated" not in block, "only this case's window"
    assert "may be WRONG" in block and "Corrects:" in block


def test_no_history_adds_nothing():
    assert responder.case_so_far("Nobody Here", "Nobody Here") == ""
