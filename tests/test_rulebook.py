"""His rules, said any way: read, applied, held at the door, withdrawn by name.

Synthetic people and alerts; the shapes are the ones he used on 20 Nov, when
two stand-ins were answered "noted" and stored nowhere, and "don't send hedwig
alert" dropped the CI draft instead.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import authority, notify, policy, rulebook, store

PRIYA, DEV, RAVI = "Priya Raman", "Dev Kumar", "Ravi Kumar"
HEDWIG = "⚠️ Still broken after 5 min:\n• Hedwig Alert (firing) - svc-x is throwing 5XX in prod"
CI = "❌ CI failed on demo-service: build #41 (unit tests)"


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    store.save_teams_messages([
        {"key": f"k{i}", "chat": c, "sender": c, "text": "hi", "sent_at": time.time() - 60, "stamp": ""}
        for i, c in enumerate((PRIYA, DEV, RAVI))])
    store.add_notification(HEDWIG, "outlook")
    store.add_notification(CI, "ci")
    monkeypatch.setattr(rulebook, "_complete", _no_model)


async def _no_model(*a, **k):
    pytest.fail("the model was asked about a message the rules already read")


@pytest.fixture
def _model_says_none(monkeypatch):
    """Messages that rightly pass the cheap gate: the model reads them, and says no."""
    async def complete(prompt, max_tokens=300, paid_ok=False):
        return '{"op": "none"}'
    monkeypatch.setattr(rulebook, "_complete", complete)


def _apply(text):
    intent = asyncio.run(rulebook.read(text))
    return rulebook.apply(intent) if intent else None


def _live_targets():
    return sorted(g.target for g in authority.grants())


# --- stand-ins ------------------------------------------------------------------

@pytest.mark.parametrize("words, who, today", [
    ("Hereafter Handle all Priya chats only one to one , going forward without my approval ..", [PRIYA], False),
    ("alll one to one interactions with Dev u handle .. going forward", [DEV], False),
    ("Auto respond all queries related to Priya for today please in one to one, "
     "no need approval from me for today", [PRIYA], True),
    ("From now on take care of Priya's chats directly", [PRIYA], False),
    ("whatever Dev asks, answer without asking me", [DEV], False),
    ("Handle Priya and Dev 1:1 going forward", [PRIYA, DEV], False),
])
def test_a_stand_in_said_his_way_is_stored(words, who, today):
    done = _apply(words)
    assert done is not None and done.intent.effect == "allow"
    assert _live_targets() == sorted(who)
    for g in authority.grants():
        assert (g.expires_at is not None) == today
        assert authority.auto_reply_to(g.target)
        assert not authority.auto_reply_to(g.target, group=True), "never a group"
    assert "Noted" not in done.line and "stop handling" in done.line


def test_same_goes_reuses_the_rule_he_just_gave():
    _apply("Hereafter handle all Priya chats going forward without my approval")
    done = _apply("Same goes to Dev as well")
    assert done is not None and _live_targets() == [DEV, PRIYA]


def test_a_name_that_fits_two_people_is_asked_never_guessed():
    done = _apply("handle all Kumar chats going forward")
    assert done.intent.op == "ask" and DEV in done.line and RAVI in done.line
    assert authority.grants() == []


def test_going_forward_widens_a_for_today_permission():
    _apply("respond to Priya directly without my approval for today")
    assert authority.grants()[0].expires_at is not None
    _apply("handle Priya's chats going forward")
    assert [g.expires_at for g in authority.grants()] == [None]


@pytest.mark.parametrize("words", [
    "send the analysis to Priya",
    "send this to Priya without my approval",          # one message, not a standing scope
    "always reply to Priya in English",                # style, not permission
    "don't reply to Priya without my approval",        # the opposite
    "reply to Priya directly",
    "what did Priya ask today?",
    "now send the same to Dev as well",
])
def test_a_one_off_or_an_opposite_is_never_a_stand_in(words, _model_says_none):
    _apply("Hereafter handle all Ravi chats going forward without my approval")  # a live 'last' rule
    assert _apply(words) is None
    assert _live_targets() == [RAVI]


# --- withdrawing, by name ---------------------------------------------------------

@pytest.mark.parametrize("words", [
    "stop handling Priya",
    "Priya — ask me first before replying",
    "withdraw Priya's stand-in",
    "revoke Priya permission",
    "don't handle Priya chats anymore",
])
def test_a_stand_in_is_withdrawn_by_name(words):
    _apply("handle all Priya chats going forward without my approval")
    _apply("handle all Dev chats going forward without my approval")
    done = _apply(words)
    assert done is not None and done.intent.op == "withdraw" and "Ended" in done.line
    assert _live_targets() == [DEV], "only the one he named"
    assert not authority.auto_reply_to(PRIYA)


def test_everyday_drop_never_ends_a_rule(_model_says_none):
    _apply("handle all Priya chats going forward without my approval")
    assert _apply("drop the draft to Priya") is None
    assert _live_targets() == [PRIYA]


def test_withdraw_by_id():
    _apply("handle all Priya chats going forward without my approval")
    gid = authority.grants()[0].id
    assert _apply(f"drop P{gid}").intent.op == "withdraw"
    assert authority.grants() == []


# --- muting what reaches his phone -------------------------------------------------

def _pushed(text, level="outlook", kind="alert", **kw):
    return asyncio.run(notify.notify(text, level, urgency="direct", kind=kind, **kw))


@pytest.fixture
def _no_delivery(monkeypatch):
    sent = []

    async def deliver(text, force=False, keys=()):
        sent.append(text)
        return {"bell": True, "whatsapp": True, "telegram": False}
    monkeypatch.setattr(notify, "deliver", deliver)
    return sent


@pytest.mark.parametrize("words, topic", [
    ("Don't send hedwig alert", "hedwig"),
    ("Don’t send headwig alerts", "hedwig"),             # his typo, fixed toward his alerts
    ("ignore ouhsing hedwig alerts", "hedwig"),          # a word no alert uses is not required
    ("Incident alerts notification you won't push", "incident"),
    ("stop sending me CI failure alerts", "ci failure"),
])
def test_an_alert_mute_said_his_way_is_stored(words, topic):
    done = _apply(words)
    assert done is not None and done.intent.effect == "mute"
    assert [r.target for r in policy.rules("mute")] == [topic]


def test_a_hedwig_mute_holds_hedwig_and_lets_ci_through(_no_delivery):
    done = _apply("Don't send hedwig alert")
    assert "would have held 1" in done.line and "Everything else still comes" in done.line
    assert _pushed(HEDWIG)["muted"]
    assert not _pushed(CI, "ci", kind="").get("muted")
    assert not _pushed("Priya Raman wrote: the hedwig dashboard is down", "teams", kind="").get("muted"), \
        "a person writing to him is never an alert"
    assert not _pushed("Mail from Priya Raman: hedwig silencing changes", "outlook", kind="").get("muted"), \
        "a person's mail rides the outlook level too, and is not an alert"
    assert not _pushed(HEDWIG + " — shall I look?", asks=True).get("muted"), "a question to him goes"
    assert policy.check("investigate", "hedwig").ok, "a push mute is not an investigate mute"


def test_a_topic_mute_never_holds_people_or_his_work(_no_delivery):
    """Live week: "incident" also appeared in 8 colleague asks and 4 task results."""
    _apply("Incident alerts notification you won't push")
    assert _pushed("🚨 Broken now:\n• INC0001 incident on svc-x")["muted"]
    for text, level in (("*Priya Raman* wants you to check incident INC0002", "answer"),
                        ("✅ Task #12 done — the incident was a config typo", "task")):
        assert not _pushed(text, level, kind="").get("muted")
    store.add_notification("✅ Task #13 done — incident closed", "task")
    assert all("Task" not in c for c in rulebook.would_have_caught("incident"))


@pytest.mark.parametrize("words", [
    "don't send this alert",        # nothing named: about a draft
    "don't send it yet",
    "Don't send the alert to Priya",
    "Ignore",
])
def test_a_draft_instruction_is_not_a_mute(words, _model_says_none):
    assert _apply(words) is None
    assert policy.rules("mute") == []


def test_send_it_again_lifts_the_mute(_no_delivery):
    _apply("Don't send hedwig alert")
    done = _apply("ok send hedwig alerts again")
    assert done.intent.op == "withdraw" and policy.rules("mute") == []
    assert not _pushed(HEDWIG).get("muted")


def test_a_mute_for_today_ends_by_itself():
    _apply("mute CI alerts for today")
    r = policy.rules("mute")[0]
    assert r.expires_at and policy.muted_push(CI, "ci")
    assert policy.muted_push(CI, "ci", now=r.expires_at + 1) is None


# --- one list, and the store underneath -------------------------------------------

def test_my_rules_lists_permissions_and_rules_together():
    from app import frontdesk
    _apply("handle all Priya chats going forward without my approval")
    _apply("Don't send hedwig alert")
    out = frontdesk.answer_from_state("my rules")
    assert "Priya Raman" in out and "hedwig" in out and "stop handling" in out


def test_two_different_notes_are_two_rules():
    """5 Oct–20 Nov: every new note came back as the first one and was never stored."""
    a = policy.add("note", words="Dont include the bot name in branch names")
    b = policy.add("note", words="Write PR titles in the imperative")
    assert a.id != b.id and len(policy.rules("note")) == 2
    assert policy.add("note", words="dont include the bot name in branch names!").id == a.id


# --- the long tail: the model, checked by code -------------------------------------

def _model_says(monkeypatch, answer):
    calls = []

    async def complete(prompt, max_tokens=300, paid_ok=False):
        calls.append(prompt)
        return json.dumps(answer)
    monkeypatch.setattr(rulebook, "_complete", complete)
    return calls


def test_the_model_reads_what_the_rules_cannot(monkeypatch):
    calls = _model_says(monkeypatch, {"op": "add", "effect": "mute", "match": "hedwig", "until": "forever"})
    done = _apply("going forward keep the hedwig noise off my phone")
    assert calls and done.intent.by == "model"
    assert [r.target for r in policy.rules("mute")] == ["hedwig"]


def test_the_model_cannot_name_someone_he_did_not(monkeypatch):
    _model_says(monkeypatch, {"op": "add", "effect": "allow", "people": ["Priya"]})
    assert _apply("going forward be quicker with replies") is None
    assert authority.grants() == []


def test_the_model_cannot_invent_a_topic(monkeypatch):
    _model_says(monkeypatch, {"op": "add", "effect": "mute", "match": "jira"})
    assert _apply("from now on fewer pings about the release please") is None


def test_a_correction_replaces_the_rule_it_corrects(monkeypatch):
    _apply("stop sending me CI failure alerts")
    rid = policy.rules("mute")[0].id
    _model_says(monkeypatch, {"op": "amend", "effect": "mute", "match": "hedwig", "ids": [f"R{rid}"]})
    done = _apply("Hey I said don't send headwig alter, not CI failure still u have to send")
    assert "Ended" in done.line
    assert [r.target for r in policy.rules("mute")] == ["hedwig"]


def test_an_ordinary_message_never_costs_a_model_call():
    for words in ("send the analysis to Priya", "check booking ABC123 in sit", "Task 12 raise PR",
                  "ok", "what's pending?"):
        assert asyncio.run(rulebook.read(words)) is None        # _no_model fails if asked


def test_brains_see_the_live_rules_and_cannot_claim_one():
    from app import guardrails
    _apply("handle all Priya chats going forward without my approval")
    block = guardrails.block("chat")
    assert "Priya Raman" in block and "Never say a rule" in block
    assert "Priya Raman" not in guardrails.block("code")


# --- the dispatcher: before any brain, and never at a waiting draft's expense ----

class _Sink:
    def __init__(self):
        self.sent = []

    async def send(self, p):
        self.sent.append(p)


def _dispatch(monkeypatch, words, staged=None):
    from app import loop, main

    def no_brain(*a, **k):
        pytest.fail("a rule needs no brain turn")
    monkeypatch.setattr(main, "_start_turn", no_brain)
    conv = store.create_conversation("claude_cli", None)
    if staged:
        loop.stage(conv["id"], staged)
    sink = _Sink()
    asyncio.run(main._dispatch(conv, words, sink, "whatsapp", skip_new_gates=True))
    return conv["id"], " ".join(p.get("text") or "" for p in sink.sent)


def test_hedwig_mute_leaves_the_waiting_ci_draft_alone(monkeypatch):
    """20 Nov: the brain read "don't send hedwig alert" as "drop the CI draft"."""
    from app import loop
    draft = {"kind": "send", "what": "CI failed on demo-service", "to": "Sam Roe", "channel": "teams"}
    cid, said = _dispatch(monkeypatch, "Don't send hedwig alert", staged=draft)
    assert "Don't push me hedwig alerts" in said
    assert loop.awaiting(cid) and loop.awaiting(cid)["what"] == draft["what"]


def test_a_stand_in_reaches_the_dispatcher_without_a_brain(monkeypatch):
    _, said = _dispatch(monkeypatch, "Hereafter handle all Priya chats going forward without my approval")
    assert "Priya Raman" in said and "until you withdraw it" in said
    assert authority.auto_reply_to(PRIYA)
