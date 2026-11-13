"""Seven gaps found by a live test of Asta's own changes (synthetic data).

1-2. The tool's description named no `files`, so the brain wrote a file path
     into the text; it went out as mangled text with no file.
3.   "only stage it, I will not send it" still counted as asking to send.
4.   A send to another chat during a turn dropped the waiting draft.
5.   A colleague's case never carried their own words.
6.   The class lookup tried only the first three names it met.
7.   A resumed session kept a tool's old schema after a deploy.
"""
from __future__ import annotations

import subprocess
import time

import pytest

from app import (agent, capabilities, debug_hints, loop, main, mcp_server, responder,
                 store, tasks)
from app import teams_bridge as tb

WHO = "Marlowe Reyes"


@pytest.fixture
def conv(monkeypatch):
    c = store.create_conversation(model="claude_cli", workspace=None)
    monkeypatch.setattr(tasks, "current_conversation", lambda: c["id"])
    return c["id"]


def _say(cid, text):
    store.add_ui_message(cid, "user", text)


# --- 1-2 ------------------------------------------------------------------------

def test_the_tool_description_names_files():
    cap = capabilities.registry()["prepare_to_send"]
    text = mcp_server._describe(cap) + cap.http
    assert "files" in text


def test_a_path_in_the_text_is_refused(conv):
    _say(conv, f"send {WHO} the sample file")
    out = agent.prepare_to_send("here it is (attachment: /Users/someone/work/a.txt)",
                                to=WHO, channel="teams")
    assert out.startswith("Not staged") and "`files`" in out
    assert not loop.take(conv)


def test_ordinary_words_with_a_slash_still_stage(conv):
    _say(conv, "draft a note")
    out = agent.prepare_to_send("deployed to sit/qa, see src/main/App.java", to=WHO, channel="teams")
    assert "Draft staged" in out


# --- 3 --------------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    f"stage a message to {WHO} saying hi — only stage it, I will not send it",
    f"tell {WHO} hi, draft only",
    f"message {WHO} hi but don't send, show me first",
])
def test_stage_only_words_are_not_an_ask_to_send(conv, said, monkeypatch):
    monkeypatch.setattr(agent, "SEND_WHEN_ASKED", True)
    _say(conv, said)
    assert agent._he_asked_to_send(WHO, conv) is False


def test_a_plain_ask_still_sends(conv, monkeypatch):
    monkeypatch.setattr(agent, "SEND_WHEN_ASKED", True)
    _say(conv, f"tell {WHO} I'm on it")
    assert agent._he_asked_to_send(WHO, conv) is True


# --- 4 --------------------------------------------------------------------------

def test_a_send_elsewhere_keeps_the_waiting_draft(conv, monkeypatch):
    staged = {"kind": "send", "what": "Sure, go ahead here.", "to": "Ledger Triage",
              "to_group": True, "channel": "teams", "who": "Pat Quill"}
    at = time.time()
    main._under_feedback[conv] = (staged, "send my own chat a test note", at)
    monkeypatch.setattr(tb, "STARTED_TO", [(at + 1, "Arunkumar K")])
    monkeypatch.setattr(tb, "SENT", [(at + 2, "Arunkumar K (You)")])
    kept = []
    monkeypatch.setattr(main, "_set_aside", lambda *a, **k: kept.append(a[1]) or True)
    main._settle_feedback(conv, None)
    assert kept and kept[0]["to"] == "Ledger Triage"


def test_a_send_to_the_drafts_chat_is_acting_on_it(conv, monkeypatch):
    staged = {"kind": "send", "what": "x", "to": "Pat Quill, Ira Moss, +2", "to_group": True}
    at = time.time()
    main._under_feedback[conv] = (staged, "make it shorter", at)
    monkeypatch.setattr(tb, "STARTED_TO", [(at + 1, "Pat Quill, Ira Moss, +3")])
    monkeypatch.setattr(tb, "SENT", [])
    kept = []
    monkeypatch.setattr(main, "_set_aside", lambda *a, **k: kept.append(a[1]) or True)
    main._settle_feedback(conv, None)
    assert not kept


def test_a_revision_sent_to_the_drafts_person_is_acting_on_it(conv, monkeypatch):
    staged = {"kind": "send", "what": "x", "to": "Ledger Triage", "to_group": True, "who": "Pat Quill"}
    at = time.time()
    main._under_feedback[conv] = (staged, "tell him I'm on it", at)
    monkeypatch.setattr(tb, "STARTED_TO", [(at + 1, "Pat Quill")])
    monkeypatch.setattr(tb, "SENT", [])
    kept = []
    monkeypatch.setattr(main, "_set_aside", lambda *a, **k: kept.append(a[1]) or True)
    main._settle_feedback(conv, None)
    assert not kept


def test_same_chat_matching():
    assert main._same_chat("Arunkumar K (You)", "Arunkumar K")
    assert main._same_chat("Pat Quill\nIra Moss\n+2", "Pat Quill, Ira Moss, +5")
    assert not main._same_chat("Pat Quill", "Pat Quill, Ira Moss, +2")
    assert not main._same_chat("Ledger Triage", "Arunkumar K")


# --- 5 --------------------------------------------------------------------------

def test_the_case_carries_their_own_words():
    chat = "Pat Quill"
    now = time.time()
    store.save_teams_messages([{"key": "p1", "chat": chat, "sender": chat, "stamp": "",
                                "text": "failed at 08:44:09 in PostLedgerActivityImpl",
                                "sent_at": now - 600}])
    t = store.create_task("Pat Quill asked: why it failed", "analysis", "x", None, teams_chat=chat)
    store.update_task(t["id"], status="done", result="ANALYSIS: looked at it")
    held = responder.case_so_far(chat, chat)
    assert "What they wrote" in held and "08:44:09" in held


# --- 6 --------------------------------------------------------------------------

def test_the_class_he_names_is_found_past_three_others(tmp_path):
    repo = tmp_path / "ledger-flow-service"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "X.java").write_text("public class PostLedgerActivityImpl {}\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    noise = "AlphaService BetaWorkflow GammaController DeltaHandler"
    block = debug_hints.hints("", "why did PostLedgerActivityImpl fail", noise, roots=[repo])
    assert "PostLedgerActivityImpl is declared in ledger-flow-service" in block
    block = debug_hints.hints("", "why did it fail", noise + " PostLedgerActivityImpl", roots=[repo])
    assert "PostLedgerActivityImpl" in block


# --- 7 --------------------------------------------------------------------------

def test_a_tool_change_starts_chat_sessions_fresh_with_a_recap(conv, monkeypatch):
    store.kv_set(f"claude_session:{conv}", "abc")
    store.kv_set("tool_schema_hash", "old")
    assert main.reset_sessions_if_tools_changed() == [conv]
    assert (store.kv_get(f"claude_session:{conv}") or "") == ""
    assert store.kv_get(f"session_recap:{conv}") == "1"
    store.kv_set(f"claude_session:{conv}", "def")
    assert main.reset_sessions_if_tools_changed() == [], "same tools: nothing reset"
    assert store.kv_get(f"claude_session:{conv}") == "def"


def test_the_first_start_resets_nothing(conv):
    store.kv_set(f"claude_session:{conv}", "abc")
    assert main.reset_sessions_if_tools_changed() == []
    assert store.kv_get(f"claude_session:{conv}") == "abc"
