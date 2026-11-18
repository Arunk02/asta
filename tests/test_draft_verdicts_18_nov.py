"""18 Nov: his "no" and "hold" on a draft were read by a brain, then overruled.

What happened (names synthetic):
  12:08 "Already merged and shared. Ignore this"   → asked again at 12:13
  12:13 "Don’t send any msg, hold on it now"        → asked again at 12:27
  12:30 a draft reached his phone six seconds after it was dropped (he had
        replied in Teams himself); his "Send" got "nothing is staged — both were
        put on hold" (not why), and "Send to Ravi" went to a brain that wrote
        and sent NEW words as him.

Now: drop / hold are decided in code before any brain runs, a decided draft is
never queued again, and "send" with nothing staged reads the shelf — the exact
draft he was shown, or why it is not waiting."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import answers, delivery, loop, main, notify, store

DRAFT = "checked it, the test compares two timestamps taken a second apart — not a code bug."
OTHER = "on hold for now, will share once the build is out."


class _Sink:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    def notes(self) -> str:
        return "\n".join(p.get("text", "") for p in self.sent)


def _draft(what=DRAFT, to="Ravi Menon", task_id=901, **extra):
    return {"type": "answer", "kind": "send", "what": what, "to": to, "channel": "teams",
            "who": to, "task_id": task_id, "need": "look into the flaky test",
            "_shown": time.time(), "_showings": 1, **extra}


@pytest.fixture
def phone(monkeypatch):
    conv = store.create_conversation(model="claude_cli", workspace=None)
    conv["model"] = "claude_cli"
    store.kv_set("wa_conversation", conv["id"])
    out = {"conv": conv, "prompts": [], "sends": [], "pushes": []}

    def start(c, prompt, sink, channel):
        out["prompts"].append(prompt)
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut

    async def run_op(op, cid, sink, channel, staged=None):
        out["sends"].append((op, staged))
        return True

    async def push(text, *a, **k):
        out["pushes"].append(text)
        return {}

    monkeypatch.setattr(main, "_start_turn", start)
    monkeypatch.setattr(main, "_run_op", run_op)
    monkeypatch.setattr(main, "_mechanical_send",
                        lambda staged: {"name": "teams_send", "to": staged.get("to"),
                                        "text": staged.get("what")})
    monkeypatch.setattr(answers, "sent", lambda *a, **k: None)
    monkeypatch.setattr(notify, "notify", push)
    return out


def _say(phone, text) -> _Sink:
    sink = _Sink()
    asyncio.run(main._dispatch(phone["conv"], text, sink, "whatsapp"))
    return sink


def _stage(phone, item):
    loop.stage(phone["conv"]["id"], item)


# --- one reading of his reply ------------------------------------------------------

@pytest.mark.parametrize("said,verdict", [
    ("Already merged and shared. Ignore this", "drop"),
    ("Don’t send any msg , hold on it now .. let me know if needed ..", "hold"),
    ("I asked u to drop the Ravi msg ..", "drop"),
    ("I asked u to put on hold", "hold"),
    ("no, drop it", "drop"),
    ("nope not needed", "drop"),
    ("don't send this one", "drop"),
    ("Don’t send it", "drop"),
    ("not now", "hold"),
    # Not a verdict: these change or redirect the draft, or ask something.
    ("no, ask him to use the develop branch", ""),
    ("don't send it to the group, send it to him 1:1", ""),
    ("make it shorter", ""),
    ("tell him I am working on it, will update once done", ""),
    ("should I drop it?", ""),
    ("Reran the failed jobs once it done inform him", ""),
    ("send the msg to ravi", ""),
    ("No send the previous msg", ""),
])
def test_his_reply_to_a_draft_is_read_once_in_code(said, verdict):
    assert main._draft_verdict(said) == verdict


# --- drop and hold never come back -------------------------------------------------

def test_ignore_this_drops_it_without_a_brain_and_it_is_never_asked_again(phone):
    _stage(phone, _draft())
    sink = _say(phone, "Already merged and shared. Ignore this")
    assert phone["prompts"] == [], "decided in code — no brain turn to overrule"
    assert "Dropped the draft to Ravi Menon" in sink.notes()
    assert loop.awaiting(phone["conv"]["id"]) is None
    answers._enqueue(_draft())                        # any path that would re-queue it
    answers._park(_draft(), time.time())
    assert answers._load_queue() == [], "a draft he dropped is not queued again"
    assert not phone["sends"]


def test_hold_parks_it_silently_and_send_to_them_sends_the_exact_words(phone):
    _stage(phone, _draft())
    sink = _say(phone, "Don’t send any msg , hold on it now .. let me know if needed ..")
    assert phone["prompts"] == [] and "On hold" in sink.notes()
    answers._enqueue(_draft())
    assert answers._load_queue() == [], "held means not asked again"
    _say(phone, "Send to Ravi")
    (op, staged), = phone["sends"]
    assert staged["what"] == DRAFT and op["text"] == DRAFT, "the words he saw, not new ones"
    assert phone["prompts"] == [], "no brain writes a fresh message under his send"


def test_an_edit_with_a_no_in_front_is_still_feedback(phone):
    _stage(phone, _draft())
    _say(phone, "no, ask him to use the develop branch")
    assert phone["prompts"] and phone["prompts"][0].startswith("Arun did NOT approve")


def test_a_no_the_brain_handled_is_not_requeued_after_the_turn(phone):
    """The fallback after a feedback turn reads ’ as ' too."""
    item = _draft()
    main._under_feedback[phone["conv"]["id"]] = (item, "ok but Don’t send it", time.time())
    main._settle_feedback(phone["conv"]["id"], None)
    assert answers._load_queue() == [] and answers.decided(item) == "dropped"


def test_a_message_about_something_else_still_keeps_the_draft(phone):
    item = _draft()
    main._under_feedback[phone["conv"]["id"]] = (item, "how many chats had new messages?", time.time())
    main._settle_feedback(phone["conv"]["id"], None)
    assert [q["what"] for q in answers._load_queue()] == [DRAFT]


# --- "send" with nothing in front of him --------------------------------------------

def test_bare_send_after_he_replied_himself_says_why_and_sends_nothing(phone):
    answers.shelve(_draft(), "answered_by_him")
    sink = _say(phone, "Send")
    assert not phone["sends"] and phone["prompts"] == []
    note = sink.notes()
    assert "you replied to Ravi Menon yourself in Teams" in note and "send to Ravi" in note
    assert "on hold" not in note.lower(), "the reason given is the real one"


def test_send_to_them_after_he_replied_himself_sends_it_as_shown(phone):
    answers.shelve(_draft(), "answered_by_him")
    _say(phone, "Send to Ravi")
    (op, staged), = phone["sends"]
    assert op["text"] == DRAFT and phone["prompts"] == []


def test_bare_send_takes_the_one_draft_set_aside_moments_ago(phone):
    """Shown, then set aside by his next ask: his "Send" is for that draft."""
    answers._enqueue({**_draft(), "_shown": time.time() - 60})
    _say(phone, "Send")
    (op, staged), = phone["sends"]
    assert op["text"] == DRAFT and answers._load_queue() == []


def test_bare_send_with_two_candidates_asks_which(phone):
    answers.shelve(_draft(), "retired")
    answers.shelve(_draft(what=OTHER, to="Asha Rao", task_id=902), "held")
    sink = _say(phone, "Send")
    assert not phone["sends"] and "Ravi Menon" in sink.notes() and "Asha Rao" in sink.notes()


def test_send_with_nothing_shelved_goes_on_as_before(phone):
    _say(phone, "Send")
    assert not phone["sends"] and phone["prompts"], "no shelf entry: unchanged path"


def test_a_staged_draft_still_wins_over_the_shelf(phone):
    answers.shelve(_draft(what=OTHER, to="Asha Rao", task_id=902), "held")
    _stage(phone, _draft())
    _say(phone, "Send")
    (op, staged), = phone["sends"]
    assert op["text"] == DRAFT


# --- a push that asks him is never batched ------------------------------------------

def test_an_asking_push_goes_out_now_and_news_still_batches(monkeypatch):
    sent, buffered = [], []

    async def deliver(text, **k):
        sent.append(text)
        return {"whatsapp": True, "telegram": False}

    monkeypatch.setattr(delivery, "should_batch", lambda *a, **k: True)
    monkeypatch.setattr(delivery, "buffer", lambda text: buffered.append(text))
    monkeypatch.setattr(notify, "deliver", deliver)
    monkeypatch.setattr(notify, "already_on_phone", lambda text: False)
    asyncio.run(notify.notify("Here's what I'd send Ravi — send it?", "answer",
                              considered=True, asks=True))
    asyncio.run(notify.notify("CI went green on develop", "ci", considered=True))
    assert sent == ["Here's what I'd send Ravi — send it?"]
    assert buffered == ["CI went green on develop"]
