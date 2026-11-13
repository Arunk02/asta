"""Where a debugging turn looks is read off the words, not guessed.

Synthetic replay of a live case: a colleague named an activity class in one
service and gave a Temporal link for one environment. The chat brain read a
different service, then switched environment on its own; a three-day log window
was read as the last 30 minutes; a dropped group reply kept steering his
messages; and the colleague's task asked his permission for something he had
asked about himself a minute earlier.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from app import agent, answers, debug_hints, debugkit, loop, main, responder, store

LINK = ("https://temporal.example/namespaces/team-sit-a/workflows?query="
        "%60WorkflowId%60+STARTS_WITH+%22QX7ZLMRBTWA%22")
CASE = ("Pat Quill: QX7ZLMRBTWA\n" + LINK + "\n"
        "Pat Quill: send to ledger failed — data violation in PostLedgerActivityImpl")


@pytest.fixture
def repos(tmp_path):
    owner = tmp_path / "ledger-flow-service"
    other = tmp_path / "intake-service"
    for repo, body in ((owner, "public class PostLedgerActivityImpl {}\n"),
                       (other, "public class IntakeMapper {}\n")):
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "X.java").write_text(body)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    return [other, owner]


def _conv():
    return store.create_conversation(model="claude_cli", workspace=None)["id"]


# --- where to look -----------------------------------------------------------------

def test_the_class_names_its_service_and_the_link_its_workflow(repos):
    cid = _conv()
    block = debug_hints.hints(cid, "check this in sit why it failed", CASE, roots=repos)
    assert "Environment: SIT" in block
    assert "team-sit-a" in block and "QX7ZLMRBTWA" in block
    assert "PostLedgerActivityImpl is declared in ledger-flow-service" in block


def test_the_environment_he_named_earlier_stays_pinned(repos):
    cid = _conv()
    store.add_ui_message(cid, "user", "QX7ZLMRBTWA check this in sit why it failed")
    block = debug_hints.hints(cid, "check the other side", CASE, roots=repos)
    assert "Environment: SIT" in block and "named at" in block


def test_the_link_sets_the_environment_when_he_named_none(repos):
    block = debug_hints.hints("", "why did it fail", CASE, roots=repos)
    assert "Environment: SIT" in block


def test_an_ordinary_turn_gets_no_block(repos):
    cid = _conv()
    store.add_ui_message(cid, "user", "deploy went fine in sit")
    assert debug_hints.hints(cid, "thanks, all good", "thanks, all good", roots=repos) == ""


def test_a_class_no_repo_declares_names_nothing(repos):
    block = debug_hints.hints("", "why the error", "error in UnknownThingImpl", roots=repos)
    assert "UnknownThingImpl" not in block


# --- his own ask counts as asking ------------------------------------------------------

def test_his_own_ask_about_the_same_id_is_in_the_colleague_brief():
    cid = _conv()
    store.kv_set("wa_conversation", cid)
    store.add_ui_message(cid, "user", "QX7ZLMRBTWA check this in sit why it failed")
    note = responder.he_asked_too(CASE)
    assert "QX7ZLMRBTWA" in note and "do not stop to ask his permission" in note
    assert note in responder.brief_for("debug", "Pat Quill", CASE)


def test_an_old_or_unrelated_ask_does_not_count():
    cid = _conv()
    store.kv_set("wa_conversation", cid)
    store.add_ui_message(cid, "user", "QX7ZLMRBTWA check this")
    assert responder.he_asked_too(CASE, now=time.time() + 2 * 3600) == ""
    assert responder.he_asked_too("Pat Quill: can you check ZZ9ZZZZZZZZ") == ""


# --- a log window in minutes is honoured -------------------------------------------------

def test_minutes_alone_is_how_far_back(monkeypatch):
    got = {}

    async def lines(env, service, terms, logger, s, e, limit, direction, pod):
        got.update(s=s, e=e)
        return {"rows": []}

    monkeypatch.setattr(debugkit, "lines", lines)
    monkeypatch.setattr(debugkit, "render_lines", lambda found: "")
    asyncio.run(agent.log_lines("sit", "ledger", ["QX7ZLMRBTWA"], minutes=4320))
    assert abs((got["e"] - got["s"]).total_seconds() - 4320 * 60) < 1
    asyncio.run(agent.log_lines("sit", "ledger", ["QX7ZLMRBTWA"]))
    assert got["s"] is None, "no window given: the default stays the default"
    asyncio.run(agent.log_lines("sit", "ledger", at="2026-11-01T10:00:00"))
    assert abs((got["e"] - got["s"]).total_seconds() - 600) < 1


# --- a dropped reply stops steering ----------------------------------------------------

def test_a_reply_he_says_no_to_forgets_where_it_was_going():
    cid = _conv()
    staged = {"kind": "send", "what": "x", "to": "Ledger Triage", "to_group": True,
              "channel": "teams", "who": "Pat Quill"}
    loop.stage(cid, staged)
    assert loop.reply_context(cid)["to"] == "Ledger Triage"
    main._under_feedback[cid] = (staged, "ignore the msg, not our scope", time.time())
    main._settle_feedback(cid, None)
    assert loop.reply_context(cid) == {}
