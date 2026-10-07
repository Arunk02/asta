"""The evening of 29 Sep, five screenshots, one complaint: it is not workable yet.

  1. Yogesh: "Arunkumar K Call ?" right after a defect discussion got "Could you
     tell me a bit more about what you need help with?" — then a SECOND generic
     question — then "🔴 Yogesh…: Discuss and understand the fix". He wanted:
     "Yogesh asked for a call; I checked, it's the corrupt-message defect. Want
     me to set it up, or will you take it?"
  2. Vinish's booking check was investigated by 19:07 and never reached him.
  4. Harika's real ask, after Asta's question, got no answer until he replied.
     Both (2) and (4) sat in a queue behind a draft he had never answered.
  3. "open youtube and play some songs" opened YouTube's home page.
  5. Talk to me, not a fixed Teams template; and for now say nothing in groups.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from test_round4_phase_d import _decide, _m, phone, rail  # noqa: F401  (fixtures)

from app import store


# --- 5. groups: analyse, never speak ---------------------------------------------------

def test_asta_never_speaks_in_a_group(rail, monkeypatch):
    monkeypatch.setenv("ASTA_GROUP_SILENT", "1")
    rail.rows["SCP Deployment"] = [_m("Rajendra Kumar", "Arunkumar also this failed")]
    rail.script["teams:SCP Deployment"] = _decide("ask", "something failed",
                                                  question="Which job failed?")
    rail.sweep()
    assert rail.sent == [], "not even a question, in a group"
    assert rail.asked, "it goes straight to analysing what was asked"


def test_a_group_opener_is_not_answered_in_the_group(rail, monkeypatch):
    monkeypatch.setenv("ASTA_GROUP_SILENT", "1")
    rail.rows["SCP Deployment"] = [_m("Rajendra Kumar", "Arunkumar hi")]
    rail.script["teams:SCP Deployment"] = _decide("opener")
    rail.sweep()
    assert rail.sent == []


def test_one_to_one_still_asks(rail, monkeypatch):
    monkeypatch.setenv("ASTA_GROUP_SILENT", "1")
    rail.rows["Vinish Kumar"] = [_m("Vinish Kumar", "also this failed")]
    rail.script["teams:Vinish Kumar"] = _decide("ask", "failed", question="Which job failed?")
    rail.sweep()
    assert rail.sent == [("Vinish Kumar", "Which job failed?")]


# --- 1. read the context before asking -------------------------------------------------

def test_an_opener_is_asked_about_with_what_is_already_known(rail):
    rail.rows["Yogesh Kumar Ravichandran"] = [_m("Yogesh Kumar Ravichandran", "Call ?")]
    rail.script["teams:Yogesh Kumar Ravichandran"] = _decide(
        "opener", question="Sure — is this about the event-history defect?")
    rail.sweep()
    assert rail.sent == [("Yogesh Kumar Ravichandran",
                          "Sure — is this about the event-history defect?")]


def test_a_call_about_a_known_subject_is_worked_before_he_hears(rail):
    """Agreed flow: clarify with them directly, do the needful, and only the
    final analysis reaches him. A call about a known defect is checked first."""
    rail.rows["Yogesh Kumar Ravichandran"] = [_m("Yogesh Kumar Ravichandran",
                                                 "Call? about the event-history defect fix")]
    rail.script["teams:Yogesh Kumar Ravichandran"] = {
        **_decide("ask", "call about the event-history defect", work="talk"),
        "subject": "stated", "tell": "Yogesh wants a call about the defect.", "reply": "Sure"}
    rail.sweep()
    assert rail.asked and rail.asked[0]["need"] == "call about the event-history defect"
    assert rail.sent == [] and not rail.pushed, "he hears once, when it is ready"


def test_the_models_words_replace_the_template(rail):
    tell = "Rajendra says PP is up and UAT follows in 15 minutes — nothing needed from you."
    rail.rows["SCP Deployment"] = [_m("Rajendra Kumar", "Arunkumar PP is up")]
    rail.script["teams:SCP Deployment"] = {**_decide("status"), "summary": "PP up",
                                           "tell": tell}
    rail.sweep()
    assert rail.pushed[0]["text"] == tell
    assert "💬 Teams" not in rail.pushed[0]["text"]


def test_the_reader_is_told_about_talk_and_tell():
    from app import understand
    assert '"talk"' in understand._INSTRUCTIONS and "tell" in understand._INSTRUCTIONS
    d = understand._clean({"state": "ask", "work": "talk", "tell": "  Yogesh wants\n a call. "},
                          understand.rules({"id": "x", "new": ["Call ?"]}))
    assert d["work"] == "talk" and d["tell"] == "Yogesh wants a call."


# --- 2 and 4. nothing waits forever behind a question he did not answer ----------------

def test_an_unanswered_draft_steps_aside(phone, monkeypatch):
    from app import answers, loop
    asyncio.run(answers.present(who="Nakka Harika", need="the 11140 filters", chat="Nakka Harika",
                                group=False, analysis="", reply="first", task_id=1))
    asyncio.run(answers.present(who="Vinish Kumar", need="booking H65ZMWX52B2",
                                chat="Vinish Kumar", group=False, analysis="",
                                reply="second", task_id=2))
    assert loop.awaiting(phone["cid"])["what"] == "first"
    later = time.time() + answers.PARK_SECONDS + 1
    monkeypatch.setattr(answers.time, "time", lambda: later)
    assert asyncio.run(answers.next_after(phone["cid"]))
    assert loop.awaiting(phone["cid"])["what"] == "second", "Vinish is no longer stuck"
    assert [q["what"] for q in answers._load_queue()] == ["first"], "Harika's comes back"


def test_one_ignored_twice_is_retired_and_he_is_told(phone, monkeypatch):
    from app import answers
    store.kv_set("answers_queue", json.dumps([
        {"type": "answer", "who": "Rajendra Kumar", "need": "build failing", "what": "x",
         "to": "SCP", "channel": "teams", "_at": time.time() - answers.STALE_SECONDS - 5}]))
    assert not asyncio.run(answers.next_after(phone["cid"]))
    assert answers._load_queue() == []
    assert "Rajendra Kumar" in phone["pushed"][-1]["text"]


def test_a_fresh_draft_still_holds_the_line(phone):
    from app import answers, loop
    asyncio.run(answers.present(who="Navya R", need="merge", chat="Navya R", group=False,
                                analysis="", reply="first", task_id=1))
    asyncio.run(answers.present(who="Vinish Kumar", need="helm", chat="Vinish Kumar",
                                group=False, analysis="", reply="second", task_id=2))
    assert not asyncio.run(answers.next_after(phone["cid"]))
    assert loop.awaiting(phone["cid"])["what"] == "first"


def test_the_decision_reads_like_a_person(phone):
    from app import answers
    asyncio.run(answers.present(who="Vinish Kumar", need="Check booking H65ZMWX52B2",
                                chat="Vinish Kumar", group=False,
                                analysis="No trace in prod or UAT logs over 7 days.",
                                reply="Checked it — no trace in logs. Is the ref right?",
                                task_id=5))
    text = phone["pushed"][0]["text"]
    assert text.startswith("*Vinish Kumar* asked about check booking H65ZMWX52B2.")
    assert "> Checked it — no trace in logs. Is the ref right?" in text
    assert "send it?" in text.lower()
    assert "———" not in text and "🔎" not in text


def test_vinish_reply_sends_without_a_second_yes_but_not_to_a_group(phone, monkeypatch):
    from app import answers, authority, chat_watch, guardrails, loop, ops
    monkeypatch.setattr(guardrails, "section",
                        lambda name: "- Vinish Kumar" if name == "automatic teams replies" else "")
    monkeypatch.setattr(chat_watch, "he_replied_since", lambda chat, since: _no_reply())
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)
        return "✅ Sent to Vinish Kumar."

    async def _no_reply():
        return False

    monkeypatch.setitem(ops.REGISTRY, "teams_send", {"run": send})
    assert authority.auto_reply_to("Vinish Kumar")
    assert not authority.auto_reply_to("Vinish Kumar", group=True)
    assert not authority.auto_reply_to("Vinish")
    assert asyncio.run(answers.present(who="Vinish Kumar", need="check this",
                                       chat="Vinish Kumar", group=False, analysis="Checked",
                                       reply="I checked the logs.", task_id=1))
    assert sent == [{"to": "Vinish Kumar", "text": "I checked the logs.", "to_group": False}]
    assert loop.awaiting(phone["cid"]) is None
    assert asyncio.run(answers.present(who="Vinish Kumar", need="check this",
                                       chat="Vinish Kumar", group=False, analysis="New evidence",
                                       reply="Correction: the email service failed.", task_id=3))
    assert len(sent) == 2, "a corrected answer to the same question is not a duplicate"
    assert asyncio.run(answers.present(who="Vinish Kumar", need="check in group",
                                       chat="Defect Triage", group=True, analysis="",
                                       reply="I checked the logs.", task_id=2,
                                       source_text="Arun, please check in group"))
    assert loop.awaiting(phone["cid"])["to"] == "Defect Triage"
    assert len(sent) == 2


def test_failed_automatic_reply_is_not_claimed_sent(phone, monkeypatch):
    from app import answers, chat_watch, guardrails, loop, ops
    monkeypatch.setattr(guardrails, "section",
                        lambda name: "- Vinish Kumar" if name == "automatic teams replies" else "")

    async def no_reply(chat, since):
        return False

    async def failed(**kwargs):
        raise RuntimeError("Teams receipt not verified")

    monkeypatch.setattr(chat_watch, "he_replied_since", no_reply)
    monkeypatch.setitem(ops.REGISTRY, "teams_send", {"run": failed})
    assert asyncio.run(answers.present(who="Vinish Kumar", need="booking", chat="Vinish Kumar",
                                       group=False, analysis="", reply="Checked.", task_id=1))
    assert loop.awaiting(phone["cid"])["to"] == "Vinish Kumar"
    assert "delivery was not confirmed" in phone["pushed"][0]["text"]


# --- 3. play means playing ---------------------------------------------------------------

@pytest.mark.parametrize("text,query,browser", [
    ("open youtube and play some songs", "popular songs", "chrome"),
    ("can try again open youtube and play some tamil songs", "tamil songs", "chrome"),
    ("play arijit songs on youtube in safari", "arijit songs", "safari"),
    ("play some music", "popular songs", "chrome"),
])
def test_play_asks_are_recognised(text, query, browser):
    from app import apps
    assert apps.open_ask(text) == ("play", query, browser)


@pytest.mark.parametrize("text", ["play the video", "open youtube", "play cricket tomorrow"])
def test_not_play_asks(text):
    from app import apps
    got = apps.open_ask(text)
    assert got is None or got[0] != "play"


def test_play_opens_the_top_video_not_the_home_page(monkeypatch):
    from app import apps
    opened = []

    async def first(q):
        return "https://www.youtube.com/watch?v=isDo6u7QQ74&autoplay=1"

    async def open_url(url, browser=""):
        opened.append((url, browser))
        return {"verified": True, "app": "Google Chrome", "said": "", "url": url}

    monkeypatch.setattr(apps, "first_video", first)
    monkeypatch.setattr(apps, "open_url", open_url)
    line = asyncio.run(apps.open_it("play", "tamil songs", "chrome"))
    assert opened == [("https://www.youtube.com/watch?v=isDo6u7QQ74&autoplay=1", "chrome")]
    assert "Playing" in line and "tamil songs" in line


def test_no_video_found_says_so_and_opens_the_results(monkeypatch):
    from app import apps
    opened = []

    async def first(q):
        return ""

    async def open_url(url, browser=""):
        opened.append(url)
        return {"verified": True, "app": "Google Chrome", "said": "", "url": url}

    monkeypatch.setattr(apps, "first_video", first)
    monkeypatch.setattr(apps, "open_url", open_url)
    line = asyncio.run(apps.play("tamil songs"))
    assert "results?search_query=tamil+songs" in opened[0] and "tap one" in line


def test_a_lone_unanswered_draft_is_not_asked_again(phone, monkeypatch):
    """Parking lets OTHER decisions through; with none waiting it only nagged."""
    from app import answers, loop
    asyncio.run(answers.present(who="Vinish Kumar", need="booking", chat="Vinish Kumar",
                                group=False, analysis="", reply="first", task_id=1))
    later = time.time() + answers.PARK_SECONDS + 1
    monkeypatch.setattr(answers.time, "time", lambda: later)
    assert not asyncio.run(answers.next_after(phone["cid"]))
    assert len(phone["pushed"]) == 1 and loop.awaiting(phone["cid"])["what"] == "first"
