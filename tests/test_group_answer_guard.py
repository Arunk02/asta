"""A group message must still address Arun when its answer is presented or sent."""

import asyncio
import json
import time

from app import answers, chat_watch, loop, main, policy, responder, store, tasks

GROUP = "Defect Triage"
WHO = "Karthik B"
UNTAGGED = ("triggered a restart - "
            "[image: /Users/arun.k.k/help/asta/data/media/x.png] "
            "see https://example.test/users/arun/incident")
TAGGED = "Arun, can you check this restart? " + UNTAGGED
RESULT = "ANALYSIS:\nInvestigated the restart.\nREPLY:\nI will follow up."


def _phone():
    conv = store.create_conversation(model="claude_cli", workspace=None)
    store.kv_set("wa_conversation", conv["id"])
    return conv


def test_group_responder_checks_raw_message_not_its_summary(monkeypatch):
    def no_spawn(*args, **kwargs):
        raise AssertionError("an unaddressed group message started a task")

    monkeypatch.setattr(tasks, "spawn", no_spawn)
    assert responder.respond("teams-chat", WHO, "Arun asked about the restart",
                             reply_to=GROUP, group=True, source_text=UNTAGGED) is None


def test_completed_untagged_group_task_does_not_notify_or_stage(monkeypatch):
    conv = _phone()
    t = store.create_task("Old group investigation", "analysis", "p", None)
    answers.remember_meta(t["id"], who=WHO, need="restart", chat=GROUP,
                          group=True, thread="", source_text=UNTAGGED)

    async def no_notification(*args, **kwargs):
        raise AssertionError("untagged group analysis reached Arun")

    from app import notify
    monkeypatch.setattr(notify, "notify", no_notification)
    assert asyncio.run(answers.present_task(t["id"], t, RESULT)) is True
    assert loop.awaiting(conv["id"]) is None
    assert not answers._load_queue()


def test_joined_task_checks_each_recipient_source_independently(monkeypatch):
    conv = _phone()
    t = store.create_task("Shared investigation", "analysis", "p", None)
    answers.remember_meta(t["id"], who=WHO, need="restart", chat=GROUP,
                          group=True, thread="", source_text=UNTAGGED)
    answers.remember_waiter(t["id"], who="Dana", need="restart",
                            chat="Support", group=True, thread="",
                            source_text="Arun, please check the restart")

    async def no_reply(*args, **kwargs):
        return False

    async def notified(*args, **kwargs):
        return None

    from app import notify
    monkeypatch.setattr(chat_watch, "he_replied_since", no_reply)
    monkeypatch.setattr(chat_watch, "their_last", lambda chat: 0)
    monkeypatch.setattr(notify, "notify", notified)
    assert asyncio.run(answers.present_task(t["id"], t, RESULT)) is True
    assert loop.awaiting(conv["id"])["to"] == "Support"
    assert not answers._load_queue()


def test_queued_untagged_answer_is_dropped_without_retirement_notification(monkeypatch):
    conv = _phone()
    item = {"type": "answer", "kind": "send", "who": WHO, "to": GROUP,
            "to_group": True, "source_text": UNTAGGED, "what": "I will follow up.",
            "task_id": 249}
    store.kv_set(answers._QUEUE, json.dumps([item]))

    async def no_notification(*args, **kwargs):
        raise AssertionError("untagged queued draft reached Arun")

    from app import notify
    monkeypatch.setattr(notify, "notify", no_notification)
    assert asyncio.run(answers.next_after(conv["id"])) is False
    assert not answers._load_queue()
    assert loop.awaiting(conv["id"]) is None


def test_queued_tagged_answer_is_still_presented(monkeypatch):
    conv = _phone()
    item = {"type": "answer", "kind": "send", "who": WHO, "to": GROUP,
            "to_group": True, "source_text": TAGGED, "what": "I will follow up.",
            "need": "restart", "task_id": 250, "_at": time.time()}
    store.kv_set(answers._QUEUE, json.dumps([item]))

    async def notified(*args, **kwargs):
        return None

    from app import notify
    monkeypatch.setattr(notify, "notify", notified)
    assert asyncio.run(answers.next_after(conv["id"])) is True
    assert loop.awaiting(conv["id"])["source_text"] == TAGGED


def test_group_code_plan_needs_an_addressed_source(monkeypatch):
    offered = []

    async def show(item):
        offered.append(item)

    monkeypatch.setattr(answers, "_show_plan", show)
    assert asyncio.run(answers.offer_plan(who=WHO, chat=GROUP, need="restart",
                                          summary="code change", thread="",
                                          group=True, source_text=UNTAGGED)) is False
    assert not offered
    assert asyncio.run(answers.offer_plan(who=WHO, chat=GROUP, need="restart",
                                          summary="code change", thread="",
                                          group=True, source_text=TAGGED)) is True
    assert offered[0]["source_text"] == TAGGED


def test_old_staged_untagged_answer_cannot_send_on_approval(monkeypatch):
    conv = _phone()
    loop.stage(conv["id"], {"type": "answer", "kind": "send", "who": WHO,
                            "to": GROUP, "to_group": True, "channel": "teams",
                            "what": "I will follow up.", "task_id": 248})

    def no_send(*args, **kwargs):
        raise AssertionError("unverified group answer reached the send operation")

    monkeypatch.setattr(main, "_mechanical_send", no_send)

    class Sink:
        def __init__(self):
            self.sent = []

        async def send(self, event):
            self.sent.append(event)

    sink = Sink()
    assert asyncio.run(main._dispatch(conv, "send", sink, "whatsapp")) is None
    assert loop.awaiting(conv["id"]) is None
    assert any("nothing was sent" in x.get("text", "") for x in sink.sent)


def test_new_mute_applies_to_already_staged_group_answer():
    source = {"type": "answer", "who": "Dana", "to": GROUP, "to_group": True,
              "source_text": TAGGED, "ask_kind": "incident"}
    assert answers.origin_allowed(source)
    policy.add("mute", act="investigate", target="incident")
    assert not answers.origin_allowed(source)


def test_after_call_send_rechecks_origin(monkeypatch):
    from app import notify, ops, teams_bridge

    monkeypatch.setattr(teams_bridge, "in_a_call", lambda: False)
    reported = []

    async def report(message, *args, **kwargs):
        reported.append(message)

    async def no_send(*args, **kwargs):
        raise AssertionError("a stale group send escaped after the call")

    monkeypatch.setattr(notify, "notify", report)
    monkeypatch.setattr(ops, "run", no_send)
    staged = {"type": "answer", "to_group": True, "to": GROUP, "who": WHO,
              "what": "I will follow up.", "task_id": 248}
    asyncio.run(main._send_after_call({"name": "teams_send", "args": {}}, "phone", staged))
    assert reported and "Nothing was sent" in reported[0]
