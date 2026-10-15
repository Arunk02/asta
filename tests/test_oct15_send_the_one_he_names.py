"""15 Oct, Sameer. Nothing he said to the waiting draft was read as a send:

  "Send to Sameer as well"         — Teams has him as "Brundavanam Subha Prabhakara
                                     Sameer"; only the FIRST name matched
  "No send the previous msg"       — the earlier wording was gone after a revision
  "Send the earlier draft message"
each became "Arun did NOT approve … Revise accordingly", the draft was re-staged
three times, and nothing went out. And "any weekend plans? going somewhere?"
started an analysis task that searched his chats, then came back four times.
"""

import asyncio
import json
import time

import pytest

from app import answers, chat_watch, loop, main, responder, store, threads

SAMEER = "Brundavanam Subha Prabhakara Sameer"


class Sink:
    def __init__(self):
        self.events = []

    async def send(self, payload):
        self.events.append(payload)

    async def close(self):
        pass


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def run(op, cid, sink, channel, staged=None):
        out.append(staged)

    monkeypatch.setattr(main, "_run_op", run)
    monkeypatch.setattr(main, "_start_turn", lambda *a, **k: out.append({"revised": a[1]}))
    return out


def _draft(cid, what, to=SAMEER):
    loop.stage(cid, {"kind": "send", "channel": "teams", "to": to, "who": to, "what": what,
                     "type": "answer"})


@pytest.mark.parametrize("said", ["Send to Sameer as well", "send sameer", "send Sameer's one",
                                  "ok send to sameer"])
def test_any_of_his_names_sends_it(sent, said):
    conv = store.create_conversation("copilot", None)
    _draft(conv["id"], "no plans yet bro")
    asyncio.run(main._dispatch(conv, said, Sink(), "whatsapp"))
    assert len(sent) == 1 and sent[0]["what"] == "no plans yet bro"


@pytest.mark.parametrize("said", ["No send the previous msg", "Send the earlier draft message",
                                  "send the original one"])
def test_the_previous_wording_is_sent_as_is(sent, said):
    conv = store.create_conversation("copilot", None)
    _draft(conv["id"], "bro no plans yet, might just chill. u?")
    _draft(conv["id"], "bro going out tmr, let's see")
    asyncio.run(main._dispatch(conv, said, Sink(), "whatsapp"))
    assert len(sent) == 1 and sent[0]["what"] == "bro no plans yet, might just chill. u?"


def test_no_earlier_version_is_said_plainly_and_nothing_is_sent(sent):
    conv = store.create_conversation("copilot", None)
    _draft(conv["id"], "only one version")
    sink = Sink()
    asyncio.run(main._dispatch(conv, "send the previous msg", sink, "whatsapp"))
    assert not sent and "no earlier version" in sink.events[0]["text"]


def test_naming_a_queued_draft_sends_that_one_and_keeps_the_other(sent):
    """WhatsApp, several drafts waiting: he sends them one by one, by name."""
    conv = store.create_conversation("copilot", None)
    _draft(conv["id"], "will review PR 1486 today", to="Vinish Kumar")
    store.kv_set(answers._QUEUE, json.dumps([
        {"type": "answer", "kind": "send", "channel": "teams", "to": SAMEER, "who": SAMEER,
         "what": "no plans yet bro", "need": "weekend plans"}]))
    asyncio.run(main._dispatch(conv, "send Sameer", Sink(), "whatsapp"))
    assert len(sent) == 1 and sent[0]["to"] == SAMEER
    # Not dropped: back at the head of the queue, and — Sameer's sent — in front of him again.
    front = loop.awaiting(conv["id"])
    queue = json.loads(store.kv_get(answers._QUEUE) or "[]")
    assert (front and front["to"] == "Vinish Kumar") or [q["to"] for q in queue] == ["Vinish Kumar"]


def test_a_name_that_is_nobody_waiting_is_ordinary_feedback(sent):
    conv = store.create_conversation("copilot", None)
    _draft(conv["id"], "no plans yet bro")
    asyncio.run(main._dispatch(conv, "send to Komal", Sink(), "whatsapp"))
    assert sent and "revised" in sent[0]


# --- small talk is his ---------------------------------------------------------------

@pytest.mark.parametrize("text", ["Any weekend plans?\nGoing somewhere?", "coming to office tomorrow?",
                                  "how are you bro", "lunch?"])
def test_small_talk_starts_no_investigation(text):
    assert responder.personal(text)
    assert responder.respond("teams-chat", "Sameer", text, reply_to="Sameer") is None


@pytest.mark.parametrize("text", ["weekend deploy plan for prod?", "any plans to merge PR 1486?",
                                  "can you check booking H7Y5BWNBNX9"])
def test_work_that_mentions_plans_is_still_work(text):
    assert not responder.personal(text)


def test_small_talk_gets_no_let_me_look_into_this():
    c = {"chat": SAMEER, "last": "Any weekend plans? Going somewhere?"}
    assert asyncio.run(chat_watch._acknowledge("teams:" + SAMEER, c)) is False


def test_the_push_reads_as_a_sentence():
    head = answers.render("Sameer", "wants to know Arun's weekend plans", "", "x").splitlines()[0]
    assert head == "*Sameer* wants to know Arun's weekend plans."


def test_a_quick_chat_is_one_conversation():
    now = time.time()
    t = threads.open("teams", SAMEER, now=now - 900)
    threads.update(t["id"], summary="office plans")
    threads.close(t["id"], why="closing", now=now - 60)
    assert threads.open("teams", SAMEER, now=now, text="Any weekend plans?")["summary"] == "office plans"
