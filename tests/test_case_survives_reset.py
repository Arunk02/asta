"""The case a turn is about survives a session reset — and no other case leaks in.

Synthetic replay of a live failure. A colleague raised a booking id in a group
chat; the chat brain found the failing line. The session was then retired for
size: the fresh one got the last six messages (the finding was seventh back) and
the two most recently handed-over cases, one of them another colleague's billing
failure. It briefed a task with that other case's times and status, and the
task "disproved" the finding. Names, ids and wording are made up.
"""
from __future__ import annotations

import time

from app import copilot_cli, main, responder, store

GROUP = "Pat Quill, Ira Moss, +2"
OTHER = "Tess Vale"
ID = "QX7ZLMRBTWA"


def _done(title, result, chat):
    t = store.create_task(title, "analysis", title, None, teams_chat=chat)
    store.update_task(t["id"], status="done", result=result)
    return t["id"]


def _world():
    now = time.time()
    store.save_teams_messages([
        {"key": "g1", "chat": GROUP, "sender": "Pat Quill", "stamp": "",
         "text": f"{ID} ledger post failed: data violation at 08:44:09 in PostLedgerActivityImpl",
         "sent_at": now - 1800}])
    _done(f"Ira Moss asked: can you check {ID}", "ANALYSIS: PostLedgerActivityImpl failed at 08:44:09", GROUP)
    # Another colleague's case, handed over more recently, about a DIFFERENT id.
    responder.note_handed(OTHER)
    store.save_teams_messages([
        {"key": "o1", "chat": OTHER, "sender": OTHER, "stamp": "",
         "text": "ledger failed for ZK4TTQW9PLM at 05:22", "sent_at": now - 900}])
    _done(f"{OTHER} asked: ledger issue", "ANALYSIS: ZK4TTQW9PLM failed 05:22-05:24 LEDGER FAILED", OTHER)


def test_an_id_finds_the_chat_that_raised_it():
    _world()
    chat, held = responder.case_for_text(f"{ID} why did it fail in sit")
    assert chat == GROUP and "08:44:09" in held
    assert "05:22" not in held


def test_a_task_brief_naming_only_the_id_gets_that_case():
    _world()
    found = responder.cases_for(f"Exact field causing the failure for {ID} only")
    assert [c for c, _ in found] == [GROUP]


def test_no_id_and_no_name_attaches_nothing():
    _world()
    assert responder.cases_for("why does it fail for this one only") == []


def _recap_after_reset(cid):
    store.kv_set(f"session_recap:{cid}", "1")
    return copilot_cli._switch_recap({"id": cid}, "Claude Code CLI")


def test_the_recap_keeps_the_finding_and_only_this_case():
    _world()
    cid = store.create_conversation(model="claude_cli", workspace=None)["id"]
    store.add_ui_message(cid, "user", f"{ID} check this in sit")
    store.add_ui_message(cid, "assistant", f"Confirmed: PostLedgerActivityImpl failed for {ID} at 08:44:09 UTC.")
    for i in range(3):
        store.add_ui_message(cid, "user", f"who wrote that code {i}")
        store.add_ui_message(cid, "assistant", f"The catch block for {ID} came from PR {i}.")
    store.add_ui_message(cid, "user", "now why only this booking")      # the current turn
    recap = _recap_after_reset(cid)
    assert "your last finding on it" in recap and "08:44:09" in recap
    assert GROUP in recap
    assert OTHER not in recap and "05:22" not in recap


def test_a_new_session_is_told_the_case_again():
    cid = store.create_conversation(model="claude_cli", workspace=None)["id"]
    store.kv_set(f"case_told:{cid}:{GROUP.lower()}", str(time.time()))
    main.rotate_sessions(cid)
    assert store.kv_like(f"case_told:{cid}:%") == {}


def test_one_group_under_several_spellings_is_one_case():
    _world()
    now = time.time()
    store.save_teams_messages([
        {"key": "g2", "chat": "Pat Quill\nIra Moss\n+2", "sender": "Pat Quill", "stamp": "",
         "text": f"{ID} still failing", "sent_at": now - 60},
        {"key": "g3", "chat": "Pat Quill, Ira Moss, +3", "sender": "Ira Moss", "stamp": "",
         "text": f"{ID} any update", "sent_at": now - 30}])
    assert len(responder.chats_raising(ID)) == 1
