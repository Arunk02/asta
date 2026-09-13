"""His rules, memory and context — Astra-class P4.

The WorkWorld sets `rules.yaml` (synthetic) and `replays` (his real
corrections, local only) prove the behaviour end to end; these pin the parts.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app import (capabilities, context_pack, frontdesk, guardrails, instructions, learn, ops,
                 people, policy, responder, store, tasks)


# --- the compiler reads the way he actually writes -----------------------------------------

@pytest.mark.parametrize("said,kind,act,target,value,unless", [
    ("Don’t check on any incidents going forward unless I ask you to monitor and analyse",
     "mute", "investigate", "incident", "", True),
    ("Dont push change blindly discuss with them", "never", "push", "", "", True),
    ("which model are you on, and remember my favourite workspace is booking.",
     "prefer", "workspace", "", "booking", False),
    ("never message the release channel", "never", "send", "release channel", "", False),
    ("don't ping Alex Kumar again unless I ask", "never", "send", "Alex Kumar", "", True),
    ("always keep plan replies under 10 lines", "note", "", "", "", False),
])
def test_standing_instructions_compile_to_typed_rules(said, kind, act, target, value, unless):
    c = instructions.compile(said)
    assert (c.kind, c.act, c.target, c.value, c.unless_asked) == (kind, act, target, value, unless)


@pytest.mark.parametrize("said", [
    "dont throw 400 in the callback",                       # one task's instruction
    "no dont have to run in local, just rerun the CT in CI",
    "why do you always stop?",                              # a question about a habit
    "raise max retries to 5",
])
def test_everything_else_is_not_a_rule(said):
    assert instructions.compile(said) is None


def test_a_proposal_is_made_once():
    said = "never message the release channel"
    assert frontdesk.standing_instruction(said) is not None
    assert frontdesk.standing_instruction(said) is None


# --- the gate -------------------------------------------------------------------------------

def test_never_blocks_and_unless_asked_yields():
    policy.add("never", "send", "Alex Kumar", unless_asked=True, words="don't ping Alex unless I ask")
    policy.add("never", "call", "Release Channel", words="never call the release channel")
    assert not policy.check("send", "Alex Kumar").ok
    assert policy.check("send", "Alex Kumar", asked=True).ok
    assert not policy.check("call", "release channel", asked=True).ok     # a flat never
    assert policy.check("send", "Dana Frost").ok
    assert "Never call Release Channel" in policy.check("call", "Release Channel").why


def test_a_mute_rule_is_the_responders_mute(monkeypatch):
    monkeypatch.setenv("ASTA_RESPOND", "1")
    policy.add("mute", "investigate", "incident", unless_asked=True)
    assert responder.muted("incident")
    assert "incident" in responder.should_respond("incident", 1, "k1")
    rid = policy.rules("mute")[0].id
    assert policy.drop(rid) and not responder.muted("incident")


def test_the_latest_default_wins():
    policy.add("prefer", "workspace", value="empv3")
    policy.add("prefer", "workspace", value="booking")
    assert policy.prefer("workspace") == "booking"
    assert len(policy.rules("prefer")) == 1


def test_an_incident_ticket_is_an_incident():
    assert responder.what_it_asks("INC0012345 is open on the booking consumer, can you check?") == "incident"


def test_a_staged_act_is_refused_at_the_moment_it_would_run():
    policy.add("never", "comment", "PROJ", words="never comment on PROJ tickets")
    ran = []

    async def fake(**kw):
        ran.append(kw)
        return "posted"

    ops.REGISTRY["jira_comment"]["run"], real = fake, ops.REGISTRY["jira_comment"]["run"]
    try:
        out = asyncio.run(ops.run({"name": "jira_comment", "args": {"key": "PROJ-1", "text": "x"}}))
    finally:
        ops.REGISTRY["jira_comment"]["run"] = real
    assert out.startswith("⛔ Not done") and "drop rule" in out and not ran


# --- his yes does all three things ------------------------------------------------------------

def test_adopting_writes_the_rule_the_guardrails_line_and_a_replay():
    c = instructions.compile("never message the release channel")
    from dataclasses import asdict
    out = asyncio.run(ops.run({"name": "rule_add", "args": {**asdict(c), "said_on": "2026-09-12"}}))
    assert out.startswith("📌 Standing rule")
    assert policy.rules("never")[0].target == "release channel"
    assert "2026-09-12: Never message release channel" in guardrails.section("Standing instructions")
    replays = list(instructions.replay_dir().glob("*.yaml"))
    assert replays and str(store.DB_PATH.parent) in str(replays[0])     # never his real data/


def test_the_shipped_example_is_never_written(monkeypatch, tmp_path):
    monkeypatch.delenv("ASTA_GUARDRAILS")
    monkeypatch.setattr(guardrails, "DEFAULT_PATH", tmp_path / "guardrails.md")
    before = guardrails.EXAMPLE_PATH.read_text()
    guardrails.append_standing("2026-09-12: a rule")
    assert guardrails.EXAMPLE_PATH.read_text() == before
    assert "- 2026-09-12: a rule" in (tmp_path / "guardrails.md").read_text()


def test_a_standing_line_lands_inside_its_section():
    guardrails.append_standing("first rule")
    guardrails.append_standing("second rule")
    guardrails.append_standing("second rule")                     # idempotent
    body = guardrails.section("Standing instructions")
    assert body.count("second rule") == 1 and body.index("first rule") < body.index("second rule")


# --- what he said this turn, on every brain path -------------------------------------------------

def test_his_words_are_found_on_the_mcp_path_too():
    conv = store.create_conversation(model="claude_cli", workspace=None)
    store.add_ui_message(conv["id"], "user", "tell Alex Kumar the fix is merged")
    token = tasks.bind_conversation(conv["id"])
    try:
        assert capabilities.said_this_turn() == "tell Alex Kumar the fix is merged"
    finally:
        tasks.unbind_conversation(token)


# --- skills grow in deltas ----------------------------------------------------------------------

def test_a_skill_update_keeps_what_earlier_runs_learned(monkeypatch, tmp_path):
    from app import skills
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(learn, "_usage", lambda: {})
    monkeypatch.setattr(learn, "_save_usage", lambda d: None)
    first = {"title": "Refresh lower-env topics", "when": "a topic change needs lower envs",
             "procedure": ["read the topic list", "apply to dev", "apply to test"],
             "pitfalls": ["ACLs are separate"], "verification": ["describe the topic"],
             "confidence": 0.8}
    second = {**first, "procedure": ["read the topic list", "apply to dev", "check consumer lag"],
              "pitfalls": ["partition count cannot shrink"], "confidence": 0.7}
    learn.write_skill(first)
    path = learn.write_skill(second)
    body = path.read_text()
    assert "apply to test" in body and "check consumer lag" in body            # nothing eroded
    assert body.count("read the topic list") == 1
    assert "ACLs are separate" in body and "partition count cannot shrink" in body
    assert "confidence: 0.80" in body


# --- people, systems and the context pack -------------------------------------------------------

def test_facts_about_what_a_job_names():
    people.add("booking-service", "owns the ETA validation", "service", "PR 1409")
    people.add("Dana Frost", "reviews the AP side", "person", "his message")
    hits = people.about("fix the ETA check in booking-service")
    assert [h["subject"] for h in hits] == ["booking-service"]
    assert "(" in people.line(hits[0]) and "PR 1409" in people.line(hits[0])


def test_a_pack_carries_the_plan_notes_rules_and_facts_under_its_cap():
    from app.graph import notes
    t = store.create_task("fix the ETA check in booking-service", "code", "p", None)
    store.kv_set(f"task_plan:{t['id']}", "STRUCTURE\n  EtaValidator  rejects late ETA\n\nPLAN READY")
    notes.add(t["id"], "the check lives in BookingService.apply")
    policy.add("never", "push", unless_asked=True, words="don't push blindly")
    people.add("booking-service", "owns the ETA validation", "service", "PR 1409")
    pack = context_pack.build(t["id"], "implement")
    for want in ("plan Arun approved", "EtaValidator", "BookingService.apply",
                 "Never push unless you ask", "owns the ETA validation"):
        assert want in pack
    assert "plan Arun approved" not in context_pack.build(t["id"], "plan")   # no plan yet to carry
    assert len(context_pack.build(t["id"], "draft")) <= context_pack.CAPS["draft"] + 10


# --- rules without a brain --------------------------------------------------------------------------

def test_rules_are_listed_and_dropped_from_the_front_desk():
    r = policy.add("never", "send", "release channel")
    assert f"{r.id}. Never message release channel" in frontdesk.answer_from_state("my rules")
    assert "dropped" in frontdesk.answer_from_state(f"drop rule {r.id}")
    assert policy.rules() == []


# --- his 60 days, replayed ------------------------------------------------------------------------

def test_replays_are_mined_from_his_own_messages():
    from app.workworld import replays
    conv = store.create_conversation(model="claude_cli", workspace=None)
    for said in ("Don’t check on any incidents going forward unless I ask",
                 "fix the ETA check please", "Dont push change blindly discuss with them"):
        store.add_ui_message(conv["id"], "user", said)
    lines = replays.from_history(days=60)
    assert len(lines) == 2 and any("incident" in x for x in lines)
    assert len(list(instructions.replay_dir().glob("*.yaml"))) == 2


def test_a_mute_given_before_rules_existed_becomes_a_rule_once():
    responder.mute("incident")
    assert [r.target for r in policy.adopt_legacy()] == ["incident"]
    assert policy.adopt_legacy() == []
    assert "Don't investigate incident asks unless you ask" in policy.summary()
