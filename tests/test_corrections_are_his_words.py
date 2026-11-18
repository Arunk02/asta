"""A lesson is learned from what Arun typed, never from Asta's own wrapper.

17 Nov: each draft he said no to reached the learner as "Arun did NOT approve
sending the draft as-is. His feedback: …" and was filed as a correction — 11
copies; and twice the local model's unfilled template ("<6 words max>") was saved."""
from __future__ import annotations

from app import memory, store


def _chat(said_before: str):
    conv = store.create_conversation(model="claude_cli", workspace=None)
    store.add_ui_message(conv["id"], "assistant", said_before, {})
    return conv["id"]


def _lessons():
    return [m for m in memory.list_memories() if m["type"] == "gotcha"]


def test_feedback_on_a_waiting_draft_is_not_a_lesson(monkeypatch):
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)
    cid = _chat("Draft for Colleague One: deployed to SIT, please check.")
    wrapped = ("Arun did NOT approve sending the draft as-is. His feedback:\nno dont send\n\n"
               "Revise accordingly — change only what he asked for")
    assert memory.learn_from_correction(cid, wrapped) is None
    assert _lessons() == []


def test_a_real_correction_is_still_learned_in_his_words(monkeypatch):
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)
    cid = _chat("It stops at the plan gate for your approval.")
    path = memory.learn_from_correction(cid, "no, why do you always stop? keep going")
    assert path and "why do you always stop" in memory.read_memory_file(path)


def test_an_echoed_template_is_never_saved(monkeypatch):
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k:
                        "TITLE: <6 words max>\nLESSON: <one sentence: what to do instead>")
    cid = _chat("I sent it to the group.")
    path = memory.learn_from_correction(cid, "no, that is wrong, ping him 1:1")
    text = memory.read_memory_file(path)
    assert "<6 words max>" not in text and "<one sentence" not in text
    assert "ping him 1:1" in text
