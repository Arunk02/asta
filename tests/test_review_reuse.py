"""A shared PR review has one verified revision and distinct recipients."""

import asyncio
import json
import time

import pytest

from app import agent, answers, capabilities, chat_watch, loop, main, ops, responder, review, store, tasks


@pytest.mark.parametrize("checks,good", [
    ([{"name": "build", "conclusion": "SUCCESS"}], True),
    ([{"name": "build", "conclusion": "FAILURE"}], False),
    ([{"name": "build", "state": "PENDING"}], False),
    ([], False),
])
def test_revision_covers_full_repo_head_and_check_state(monkeypatch, checks, good):
    async def gh(cwd, *args, timeout=0):
        assert args[3:5] == ("-R", "acme/booking")
        return 0, json.dumps({"headRefOid": "a" * 40, "state": "OPEN",
                              "isDraft": False, "statusCheckRollup": checks})

    monkeypatch.setattr(review, "_gh", gh)
    revision, reusable = asyncio.run(review.revision("acme/booking#1409"))
    assert revision.startswith("acme/booking#1409@" + "a" * 40)
    assert reusable is good
    with pytest.raises(ValueError):
        asyncio.run(review.revision("1409"))


def test_only_verified_matching_pr_reuses_completed_review(monkeypatch):
    monkeypatch.setenv("ASTA_RESPOND", "1")
    monkeypatch.setattr(responder, "familiar", lambda text: (True, "known"))
    spawned = []

    def spawn(title, prompt, kind, workspace, **kw):
        t = store.create_task(title, kind, prompt, workspace)
        spawned.append(t["id"])
        return t

    monkeypatch.setattr(tasks, "spawn", spawn)
    ask = "please review my PR https://github.com/acme/booking/pull/1409"
    first = responder.respond("teams-chat", "Shabda", ask, priority=1, key="ask-1",
                              reply_to="Shabda", review_revision="acme/booking#1409@" +
                              "a" * 40 + ":green")
    assert first and len(spawned) == 1
    store.update_task(first["id"], status="done", result="ANALYSIS:\nFine\nREPLY:\nLooks good.",
                      finished_at=time.time())
    second = responder.respond("teams-chat", "Vinish", ask, priority=1, key="ask-2",
                               reply_to="Vinish", review_revision="acme/booking#1409@" +
                               "a" * 40 + ":green")
    assert second and second["reused"] and len(spawned) == 1
    responder.respond("teams-chat", "Asha", ask, priority=1, key="ask-3",
                      reply_to="Asha", review_revision="other/booking#1409@" +
                      "a" * 40 + ":green")
    responder.respond("teams-chat", "Ravi", ask, priority=1, key="ask-4",
                      reply_to="Ravi", review_revision="acme/booking#1409@" +
                      "b" * 40 + ":green")
    responder.respond("teams-chat", "Sam", ask, priority=1, key="ask-5",
                      reply_to="Sam", review_revision="acme/booking#1409@" +
                      "a" * 40 + ":red")
    assert len(spawned) == 4
    assert not responder.respond("teams-chat", "Vinish", ask, priority=1, key="ask-6",
                                 reply_to="Vinish", review_revision="").get("reused")


def test_joined_review_delivers_to_each_chat(monkeypatch):
    monkeypatch.setenv("ASTA_RESPOND", "1")
    monkeypatch.setattr(responder, "familiar", lambda text: (True, "known"))
    monkeypatch.setattr(tasks, "spawn", lambda title, prompt, kind, ws, **kw:
                        store.create_task(title, kind, prompt, ws))
    cid = store.create_conversation(model="claude_cli", workspace=None)["id"]
    store.kv_set("wa_conversation", cid)
    key = "acme/booking#1409@" + "a" * 40 + ":green"
    ask = "please review my PR acme/booking#1409"
    first = responder.respond("teams-chat", "Shabda", ask, priority=1, key="a",
                              reply_to="Shabda", review_revision=key)
    second = responder.respond("teams-chat", "Vinish", ask, priority=1, key="b",
                               reply_to="Vinish", review_revision=key)
    assert second["joined"] and second["id"] == first["id"]
    assert answers._review_waiters(first["id"])[0]["chat"] == "Vinish"

    async def current(origin):
        return True

    async def not_replied(chat, since):
        return False

    async def notified(*a, **k):
        pass

    from app import notify
    monkeypatch.setattr(answers, "review_is_current", current)
    monkeypatch.setattr(chat_watch, "he_replied_since", not_replied)
    monkeypatch.setattr(chat_watch, "their_last", lambda chat: time.time() - 60)
    monkeypatch.setattr(notify, "notify", notified)
    result = "ANALYSIS:\nNo blocking issues.\nREPLY:\nLooks good to me."
    assert asyncio.run(answers.present_task(first["id"], first, result))
    assert loop.awaiting(cid)["to"] == "Shabda"
    assert [w["to"] for w in answers._load_queue()] == ["Vinish"]


def test_approved_generic_review_reply_reaches_second_chat_without_another_yes(monkeypatch):
    cid = store.create_conversation(model="claude_cli", workspace=None)["id"]
    store.kv_set("wa_conversation", cid)
    origin = {"ref": "acme/booking#1409", "revision": "acme/booking#1409@" +
              "a" * 40 + ":green"}
    store.kv_set("review_origin:91", json.dumps(origin))
    store.save_teams_messages([
        {"key": "q1", "chat": "Shabda", "sender": "Shabda", "text": "review?", "sent_at": time.time() - 90},
        {"key": "q2", "chat": "Vinish", "sender": "Vinish", "text": "review?", "sent_at": time.time() - 80},
    ])
    sent = []

    async def current(value):
        return True

    async def no_reply(chat, since):
        return False

    async def run(op):
        sent.append(op)
        return "Sent to Vinish."

    async def notified(*a, **k):
        pass

    from app import notify
    monkeypatch.setattr(answers, "review_is_current", current)
    monkeypatch.setattr(chat_watch, "he_replied_since", no_reply)
    monkeypatch.setattr(chat_watch, "their_last", lambda chat: time.time() - 90)
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(notify, "notify", notified)
    for who in ("Shabda", "Vinish"):
        assert asyncio.run(answers.present(who=who, need="review PR", chat=who,
                                           group=False, analysis="No issues",
                                           reply="Looks good to me.", task_id=91))
    assert not sent
    assert loop.awaiting(cid)["to"] == "Shabda"
    assert answers._load_queue()[0]["to"] == "Vinish"
    approved = loop.awaiting(cid)
    loop.clear_awaiting(cid)
    answers.sent(approved)
    asyncio.run(answers.next_after(cid))
    assert len(sent) == 1 and sent[0]["args"]["to"] == "Vinish"
    assert loop.awaiting(cid) is None


def test_posting_an_approved_review_rechecks_head_and_ci(monkeypatch):
    calls = []
    origin = {"ref": "acme/booking#1409",
              "revision": "acme/booking#1409@" + "a" * 40 + ":green"}

    async def current(ref):
        return "acme/booking#1409@" + "b" * 40 + ":green", True

    async def post(*args):
        calls.append(args)
        return "Posted"

    monkeypatch.setattr(review, "revision", current)
    monkeypatch.setattr(review, "post_inline_review", post)
    with pytest.raises(RuntimeError, match="changed"):
        asyncio.run(ops._pr_review_inline("acme/booking#1409", action="approve",
                                          review_origin=origin))
    assert not calls


def test_task_stages_review_for_its_verified_pr_even_if_worker_uses_bare_number(monkeypatch):
    origin = {"ref": "acme/booking#1409",
              "revision": "acme/booking#1409@" + "a" * 40 + ":green"}
    store.kv_set("review_origin:27", json.dumps(origin))

    async def current(ref):
        assert ref == origin["ref"]
        return origin["revision"], True

    monkeypatch.setattr(review, "revision", current)
    token = capabilities.FROM_TASK.set("27")
    try:
        said = asyncio.run(agent.propose_pr_review("1409", "VERDICT: APPROVE — no issues."))
    finally:
        capabilities.FROM_TASK.reset(token)
    from app import offers
    pending = offers.pending()
    assert "Staged" in said and pending.op["args"]["pr"] == origin["ref"]
    assert pending.op["args"]["review_origin"] == origin


def test_review_reply_not_automatically_reworded_for_another_person(monkeypatch):
    cid = store.create_conversation(model="claude_cli", workspace=None)["id"]
    store.kv_set("wa_conversation", cid)
    origin = {"ref": "acme/booking#1409",
              "revision": "acme/booking#1409@" + "a" * 40 + ":green"}
    store.kv_set("review_origin:27", json.dumps(origin))

    async def current(value):
        return True

    async def no_reply(chat, since):
        return False

    async def notified(*a, **k):
        pass

    async def never(op):
        raise AssertionError("a personalized answer must not be sent to someone else")

    from app import notify
    monkeypatch.setattr(answers, "review_is_current", current)
    monkeypatch.setattr(chat_watch, "he_replied_since", no_reply)
    monkeypatch.setattr(chat_watch, "their_last", lambda chat: time.time() - 60)
    monkeypatch.setattr(notify, "notify", notified)
    monkeypatch.setattr(ops, "run", never)
    answers.sent({"type": "answer", "to": "Shabda", "who": "Shabda", "what": "Shabda, looks fine.",
                  "review_origin": origin})
    assert asyncio.run(answers.present(who="Vinish", need="review", chat="Vinish", group=False,
                                       analysis="No issues", reply="Shabda, looks fine.",
                                       task_id=27))
    assert loop.awaiting(cid)["to"] == "Vinish"


def test_stale_review_draft_never_sends_even_after_his_yes(monkeypatch):
    origin = {"ref": "acme/booking#1409",
              "revision": "acme/booking#1409@" + "a" * 40 + ":green"}
    staged = {"type": "answer", "to": "Vinish", "what": "Looks good.",
              "review_origin": origin}

    async def stale(value):
        return False

    async def never(op):
        raise AssertionError("the outdated review must never be posted")

    class Sink:
        def __init__(self):
            self.messages = []

        async def send(self, data):
            self.messages.append(data)

    monkeypatch.setattr(answers, "review_is_current", stale)
    monkeypatch.setattr(ops, "run", never)
    sink = Sink()
    op = {"name": "teams_send", "args": {"to": "Vinish", "text": "Looks good."}}
    assert asyncio.run(main._run_op(op, "phone", sink, "web", staged=staged)) is False
    assert sink.messages[-1]["type"] == "done"
    assert any("Nothing sent" in x.get("text", "") for x in sink.messages)


def test_failed_send_cannot_authorize_the_next_colleague(monkeypatch):
    origin = {"ref": "acme/booking#1409",
              "revision": "acme/booking#1409@" + "a" * 40 + ":green"}
    staged = {"type": "answer", "to": "Shabda", "who": "Shabda",
              "what": "Looks good.", "review_origin": origin}
    cid = store.create_conversation(model="claude_cli", workspace=None)["id"]

    async def current(value):
        return True

    async def fail(op):
        raise RuntimeError("Teams disconnected")

    class Sink:
        async def send(self, data):
            pass

    monkeypatch.setattr(answers, "review_is_current", current)
    monkeypatch.setattr(ops, "run", fail)
    monkeypatch.setattr(main.teams_bridge, "in_a_call", lambda: False)
    assert asyncio.run(main._run_op({"name": "teams_send", "args": {}}, cid, Sink(),
                                    "whatsapp", staged=staged)) is False
    assert loop.awaiting(cid) == staged
    assert not store.kv_get(answers._approved_review_key(origin))
