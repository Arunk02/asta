"""Concurrent phone messages remain separately routed, owned and deliverable."""

from __future__ import annotations

import asyncio

from app import loop, main, offers, store


class Sink:
    def __init__(self):
        self.events = []

    async def send(self, payload):
        self.events.append(payload)

    async def close(self):
        pass


def test_deferred_message_is_redispatched_with_its_own_sink(monkeypatch):
    conv = store.create_conversation("copilot", None)
    first, second = Sink(), Sink()
    release = asyncio.Event()
    entered = asyncio.Event()
    seen = []

    async def brain(sink, current, text, channel):
        seen.append((text, sink))
        if sink is first:
            entered.set()
            await release.wait()
        else:
            await sink.send({"type": "note", "text": "second answer"})

    monkeypatch.setattr(main, "_run_turn", brain)

    async def go():
        job = main._start_turn(conv, "analyse booking logs", first, "web")
        await entered.wait()
        result = await main._dispatch(conv, "new task: check the AP incident",
                                      second, "web")
        assert result is None
        assert store.pending_followup_conversations() == [conv["id"]]
        release.set()
        await job
        followup = main._inflight.get(conv["id"])
        if followup:
            await followup
        await asyncio.sleep(0)

    asyncio.run(go())
    assert len(seen) == 2
    assert seen[0] == ("analyse booking logs", first)
    assert seen[1] == ("new task: check the AP incident", second)
    assert any(e.get("text") == "second answer" for e in second.events)
    assert not any(e.get("text") == "second answer" for e in first.events)
    assert store.pending_followup_conversations() == []
    assert store.orphaned_followups() == []


def test_queued_whatsapp_reply_hands_off_to_push_without_repeating_ack(monkeypatch):
    conv = store.create_conversation("copilot", None)
    release = asyncio.Event()
    entered = asyncio.Event()
    pushed = []

    async def wa_send(text, **kw):
        pushed.append((text, kw))
        return True

    async def brain(sink, current, text, channel):
        if text == "first":
            entered.set()
            await release.wait()
        else:
            await sink.send({"type": "note", "text": "followup answer"})

    monkeypatch.setattr(main, "_run_turn", brain)

    async def go():
        primary = main._start_turn(conv, "first", Sink(), "web")
        await entered.wait()
        incoming = main.HybridSink(wa_send, conv["id"])
        await main._dispatch(conv, "new task: check the AP incident", incoming,
                             "whatsapp")
        ack = incoming.text()
        assert "separate task" in ack
        release.set()
        await primary
        pending = main._inflight.get(conv["id"])
        if pending:
            await pending
        await asyncio.sleep(0)

    asyncio.run(go())
    assert len(pushed) == 1
    assert "followup answer" in pushed[0][0]
    assert "separate task" not in pushed[0][0]
    assert pushed[0][1].get("done") is True


def test_restart_replays_only_messages_not_started(monkeypatch):
    conv = store.create_conversation("copilot", None)
    replayed = []

    async def dispatch(current, text, sink, channel, *, skip_new_gates=False):
        replayed.append((current["id"], text, channel, skip_new_gates))

    monkeypatch.setattr(main, "_dispatch", dispatch)
    store.enqueue_followup(conv["id"], "read this PR", "whatsapp")
    started = store.enqueue_followup(conv["id"], "ship this PR", "whatsapp")
    claimed = store.claim_followup(conv["id"])
    assert claimed["content"] == "read this PR"
    # A restarted process cannot tell whether a started external action happened.
    # It reports uncertainty and never retries that message automatically.
    told = []

    async def notify(text, kind, **kw):
        told.append(text)

    monkeypatch.setattr(main.notify, "notify", notify)

    async def go():
        await main._recover_followups()

    asyncio.run(go())
    assert len(told) == 1 and "read this PR" in told[0]
    assert replayed == [(conv["id"], "ship this PR", "whatsapp", True)]
    assert store.orphaned_followups() == []
    assert started > claimed["id"]


def test_combined_approvals_target_both_tasks_not_the_open_question(monkeypatch):
    conv = store.create_conversation("copilot", None)
    question = store.create_question("Which VTS approach: 1 or 2?", source="VTS")
    seen = []

    async def approve(verb, tid, cid):
        seen.append((verb, tid, cid))
        return f"#{tid}: accepted"

    monkeypatch.setattr(main, "_run_task_command", approve)

    async def go():
        sink = Sink()
        await main._dispatch(conv, "approve both 219 and 220", sink, "whatsapp")
        return sink

    sink = asyncio.run(go())
    assert seen == [("approve", 219, conv["id"]), ("approve", 220, conv["id"])]
    assert store.get_question(question["id"])["status"] == "open"
    assert "#219: accepted" in str(sink.events) and "#220: accepted" in str(sink.events)


def test_explicit_task_command_beats_open_share_destination(monkeypatch):
    conv = store.create_conversation("copilot", None)
    offered = offers.offer("share_build", "PR raised", "https://gh/pull/11",
                           "Where to share?")
    seen = []

    async def approve(verb, tid, cid):
        seen.append((verb, tid, cid))
        return f"#{tid}: accepted"

    monkeypatch.setattr(main, "_run_task_command", approve)
    sink = Sink()
    asyncio.run(main._dispatch(conv, "approve 219 and 220", sink, "whatsapp"))
    assert seen == [("approve", 219, conv["id"]), ("approve", 220, conv["id"])]
    assert offers.pending() == offered


def test_explicit_answer_beats_open_share_destination():
    conv = store.create_conversation("copilot", None)
    question = store.create_question("Which option?", source="booking")
    offered = offers.offer("share_build", "PR raised", "https://gh/pull/11",
                           "Where to share?")
    sink = Sink()
    asyncio.run(main._dispatch(conv, f"answer {question['id']} option 2", sink,
                               "whatsapp"))
    assert store.get_question(question["id"])["status"] != "open"
    assert offers.pending() == offered


def test_cancelling_previous_turn_does_not_start_followup_before_redirect(monkeypatch):
    conv = store.create_conversation("copilot", None)
    first, redirect, deferred = Sink(), Sink(), Sink()
    entered = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def conduct(current, text, sink, channel):
        seen.append(text)
        if text == "first":
            entered.set()
            await asyncio.Event().wait()
        if text == "redirect":
            await release.wait()

    monkeypatch.setattr(main, "_conduct", conduct)

    async def go():
        job = main._start_turn(conv, "first", first, "web")
        await entered.wait()
        main._queue_followup(conv["id"], "deferred", deferred, "web")
        job.cancel()
        try:
            await job
        except asyncio.CancelledError:
            pass
        assert seen == ["first"]
        replacement = main._start_turn(conv, "redirect", redirect, "web")
        await asyncio.sleep(0)
        assert seen == ["first", "redirect"]
        release.set()
        await replacement
        pending = main._inflight.get(conv["id"])
        if pending:
            await pending
        await asyncio.sleep(0)

    asyncio.run(go())
    assert seen == ["first", "redirect", "deferred"]
    assert store.pending_followup_conversations() == []


def test_explicit_send_to_staged_colleague_needs_no_second_approval(monkeypatch):
    conv = store.create_conversation("copilot", None)
    sent = []

    async def run(op, cid, sink, channel, staged=None):
        sent.append(staged)

    monkeypatch.setattr(main, "_run_op", run)
    loop.stage(conv["id"], {"kind": "send", "channel": "teams", "to": "Vinish Kumar",
                            "what": "Checking, will update you"})
    asyncio.run(main._dispatch(conv, "send to Vinish", Sink(), "whatsapp"))
    assert len(sent) == 1 and sent[0]["to"] == "Vinish Kumar"
    assert loop.awaiting(conv["id"]) is None


def test_a_new_request_does_not_approve_a_staged_draft(monkeypatch):
    conv = store.create_conversation("copilot", None)
    sent = []
    monkeypatch.setattr(main, "_run_op", lambda *a, **k: sent.append(k))
    monkeypatch.setattr(main, "_start_turn", lambda *a, **k: None)
    loop.stage(conv["id"], {"kind": "send", "channel": "teams", "to": "Vinish Kumar",
                            "what": "Checking, will update you"})
    asyncio.run(main._dispatch(conv, "new task: look at the AP PR", Sink(), "whatsapp"))
    assert not sent
    assert loop.awaiting(conv["id"]) is not None
