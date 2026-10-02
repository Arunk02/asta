"""Voice mode: two switches — Asta's voice (⌃⌥A) and Asta's mic (⌃⌥M).

His design, 2 Oct: voice on with the mic off is the film-on case — Asta can
tell him things and nothing in the room can reach it; both on is a
conversation; both off is WhatsApp, as before. Every rule below is one he set:
start off, release the mic, close it when idle, never butt into his talk with
someone else, never let voice alone approve what needs a tap.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app import main, notify, store, voice_mode as vm


class _Helper:
    """The menu-bar app, as the server sees it: a websocket."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def said(self) -> list[str]:
        return [m["text"] for m in self.sent if m.get("type") == "say"]


@pytest.fixture
def helper(monkeypatch):
    h = _Helper()
    vm._STATE.update(speaker=False, mic=False, busy=False, mic_on_at=0.0, last_heard=0.0,
                     last_spoke=0.0, barged_at=0.0, helper=h)
    vm._QUEUE.clear()

    async def speak(text, **k):
        return b"RIFFfake"

    from app import voice
    monkeypatch.setattr(voice, "speak", speak)
    yield h
    vm._STATE.update(speaker=False, mic=False, busy=False, helper=None)
    vm._QUEUE.clear()


def run(coro):
    return asyncio.run(coro)


# --- the switches -----------------------------------------------------------------------

def test_both_switches_start_off_and_he_hears_if_they_had_been_on():
    vm._STATE.update(speaker=True, mic=True)
    store.kv_set(vm._KV, json.dumps({"speaker": True, "mic": True}))
    assert vm.startup() is True
    assert vm.state()["speaker"] is False and vm.state()["mic"] is False
    assert vm.startup() is False, "already off: nothing to tell"


def test_a_switch_tells_the_helper_so_the_icon_and_chime_follow(helper):
    run(vm.toggle("speaker", why="hotkey"))
    run(vm.toggle("mic", why="hotkey"))
    states = [m for m in helper.sent if m["type"] == "state"]
    assert states[-1]["speaker"] and states[-1]["mic"] and states[-1]["why"] == "hotkey"


@pytest.mark.parametrize("said, which, value", [
    ("voice on", "speaker", True), ("Voice off", "speaker", False),
    ("mic on", "mic", True), ("asta mic off", "mic", False), ("speaker on.", "speaker", True),
])
def test_he_can_flip_them_from_whatsapp(helper, said, which, value):
    reply = run(vm.command(said))
    assert reply and vm.state()[which] is value


def test_ordinary_messages_are_not_switch_commands(helper):
    for said in ("turn the voice note on for Vinish", "is my mic on in the call?", "voice"):
        assert run(vm.command(said)) == ""


def test_a_switch_without_the_menu_bar_app_says_so():
    vm._STATE.update(helper=None)
    assert "not running" in run(vm.command("voice on"))


def test_the_chat_route_answers_the_switch_without_a_brain(helper, monkeypatch):
    def no_brain(*a, **k):
        raise AssertionError("a brain was asked to flip a switch")

    monkeypatch.setattr(main, "_start_turn", no_brain)

    class Sink:
        def __init__(self):
            self.sent = []

        async def send(self, p):
            self.sent.append(p)

    conv = store.create_conversation(model="claude_cli", workspace=None)
    sink = Sink()
    run(main._dispatch(conv, "voice on", sink, "whatsapp"))
    assert vm.state()["speaker"] is True and "Voice on" in str(sink.sent)


# --- what is said, and how --------------------------------------------------------------

def test_an_answer_is_said_short_without_links_code_or_markup():
    text = ("*Booking PR 1429* is still open — Vinish hasn't reviewed yet. AP PR 1252 is "
            "mergeable. Both need a review.\nhttps://github.com/x/y/pull/1429\n```\ncode\n```")
    said = vm.speakable(text)
    assert "http" not in said and "*" not in said and "code" not in said
    assert said.startswith("Booking PR 1429 is still open")
    assert said.endswith("The details are in the chat.")


def test_hindi_is_said_as_written():
    assert vm.speakable("ठीक है, मैं देखता हूँ।") == "ठीक है, मैं देखता हूँ।"


def test_with_voice_on_an_update_is_said_instead_of_pushed(helper, monkeypatch):
    pushed: list[str] = []

    async def deliver(text, **k):
        pushed.append(text)
        return {"whatsapp": True}

    monkeypatch.setattr(notify, "deliver", deliver)
    vm._STATE.update(speaker=True)
    out = run(notify.notify("✅ Task #205 done — no H69 booking in UAT either.", "task",
                            urgency="direct", considered=True))
    assert out.get("spoken") and pushed == []
    assert helper.said() and helper.sent[-1]["chime"] is True


def test_a_question_still_goes_to_his_phone_while_the_mic_is_off(helper, monkeypatch):
    pushed: list[str] = []

    async def deliver(text, **k):
        pushed.append(text)
        return {"whatsapp": True}

    monkeypatch.setattr(notify, "deliver", deliver)
    vm._STATE.update(speaker=True, mic=False)
    run(notify.notify("Vinish asked for a 2-min call. Shall I send “give me 5 mins”?", "answer",
                      urgency="direct", considered=True))
    assert helper.said(), "said aloud"
    assert pushed, "and on WhatsApp — a tap is the only way he can answer it"


def test_what_does_not_concern_him_is_never_said(helper, monkeypatch):
    async def deliver(text, **k):
        return {"whatsapp": True}

    monkeypatch.setattr(notify, "deliver", deliver)
    vm._STATE.update(speaker=True)
    run(notify.notify("Health check — LM Studio not running", "health", urgency="direct",
                      considered=True))
    run(notify.notify("CI green on someone's branch", "ci", urgency="ambient", considered=True))
    assert helper.said() == []


def test_voice_off_means_whatsapp_exactly_as_before(helper, monkeypatch):
    pushed: list[str] = []

    async def deliver(text, **k):
        pushed.append(text)
        return {"whatsapp": True}

    monkeypatch.setattr(notify, "deliver", deliver)
    run(notify.notify("✅ Task #205 done", "task", urgency="direct", considered=True))
    assert pushed and helper.said() == []


def test_during_a_call_updates_wait_and_come_as_one_summary(helper):
    vm._STATE.update(speaker=True)
    run(vm.set_busy(True))
    run(vm.say("Task 205 is done.", kind="update"))
    run(vm.say("CI is red on booking PR 1429.", kind="update"))
    assert helper.said() == [], "nothing over his call"
    run(vm.set_busy(False))
    assert len(helper.said()) == 1 and "2 updates" in helper.said()[0]


def test_without_the_asta_voice_the_mac_voice_reads_the_text(helper, monkeypatch):
    from app import voice

    async def down(text, **k):
        raise RuntimeError("voicebox is down")

    monkeypatch.setattr(voice, "speak", down)
    vm._STATE.update(speaker=True)
    assert run(vm.say("Task 205 is done."))
    assert helper.sent[-1]["audio"] == "" and helper.sent[-1]["text"] == "Task 205 is done."


# --- what is heard ----------------------------------------------------------------------

@pytest.mark.parametrize("said", ["Thank you.", "you", "Hmm", "uh", "", "the", "Yeah.", "okay",
                                  "Go Go Go Go Go Go", "Продолжение следует..."])
def test_noise_and_silence_are_never_turns(said):
    assert vm.is_noise(said)


@pytest.mark.parametrize("said", ["send", "Yes.", "no", "approve", "what's pending",
                                  "Asta"])
def test_a_one_word_answer_is_an_answer(said):
    assert not vm.is_noise(said)


def test_go_offline_closes_the_mic(helper):
    vm._STATE.update(speaker=True, mic=True)
    out = run(vm.handle("Asta, go offline"))
    assert out["did"] == "mic_off" and vm.state()["mic"] is False
    assert helper.said() == ["Okay, mic off."]


def test_be_quiet_turns_the_voice_off(helper):
    vm._STATE.update(speaker=True, mic=True)
    assert run(vm.handle("stop talking please"))["did"] == "speaker_off"
    assert vm.state()["speaker"] is False


def test_with_the_mic_off_nothing_heard_is_acted_on(helper):
    vm._STATE.update(speaker=True, mic=False)
    assert run(vm.handle("what is pending"))["did"] == "ignored"


def test_talk_with_someone_else_is_not_for_asta(helper, monkeypatch):
    from app import memory

    async def verdict(prompt, max_tokens=8, timeout=25):
        return "OTHER"

    monkeypatch.setattr(memory, "quick_verdict", verdict)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time() - 600,
                     last_heard=time.time() - 600, last_spoke=time.time() - 600)
    assert run(vm.handle("yeah I'll send you the deck after lunch"))["did"] == "not_for_asta"
    assert run(vm.meant_for_asta("Asta what's pending")), "his name always counts"


def test_a_follow_up_within_the_window_needs_no_name(helper, monkeypatch):
    from app import memory

    async def verdict(prompt, max_tokens=8, timeout=25):
        raise AssertionError("no check needed inside the window")

    monkeypatch.setattr(memory, "quick_verdict", verdict)
    vm._STATE.update(last_spoke=time.time() - 20)
    assert run(vm.meant_for_asta("and check UAT as well"))


def test_a_sentence_for_asta_runs_through_the_same_pipeline_and_is_answered_aloud(helper, monkeypatch):
    seen: list[tuple] = []

    async def dispatch(conv, text, sink, channel):
        seen.append((text, channel))
        await sink.send({"type": "delta", "text": "Booking PR 1429 is open. AP PR 1252 is open."})
        return None

    monkeypatch.setattr(main, "_dispatch", dispatch)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    out = run(vm.handle("what's pending with my PRs"))
    assert out["did"] == "answered" and seen == [("what's pending with my PRs", "voice")]
    assert helper.said() == ["Booking PR 1429 is open.", "AP PR 1252 is open."], \
        "the first sentence goes the moment it is written"


def test_a_long_turn_says_on_it_once_then_the_answer(helper, monkeypatch):
    monkeypatch.setattr(vm, "ACK_SECONDS", 0.05)

    async def dispatch(conv, text, sink, channel):
        async def work():
            await asyncio.sleep(0.2)
            await sink.send({"type": "delta", "text": "No H69 booking in UAT."})
        return asyncio.ensure_future(work())

    monkeypatch.setattr(main, "_dispatch", dispatch)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    run(vm.handle("check H69LMCN6KZY in UAT"))
    assert helper.said() == ["On it.", "No H69 booking in UAT."]


def test_idle_rule_closes_the_mic(helper, monkeypatch):
    told: list[str] = []

    async def note(text, *a, **k):
        told.append(text)
        return {}

    monkeypatch.setattr(notify, "notify", note)
    vm._STATE.update(mic=True, last_heard=time.time() - vm.MIC_IDLE_SECONDS - 5)
    calls = {"n": 0}
    real = asyncio.sleep

    async def tick(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError
        await real(0)

    monkeypatch.setattr(vm.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        run(vm.idle_loop())
    assert vm.state()["mic"] is False and told and "Mic off" in told[0]


# --- the helper's connection ------------------------------------------------------------

def test_the_websocket_wants_the_token_and_speaks_the_protocol(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("ASTA_TOKEN", "t0k")
    vm._STATE.update(speaker=False, mic=False, helper=None)
    client = TestClient(main.app)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/voice-mode?token=wrong") as ws:
            ws.receive_text()

    async def transcribe(data, filename="", language=""):
        assert data == b"RIFFwav"
        return "what is pending"

    from app import voice
    monkeypatch.setattr(voice, "transcribe", transcribe)
    with client.websocket_connect("/ws/voice-mode?token=t0k") as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == "state" and first["speaker"] is False and first["helper"] is True
        ws.send_text(json.dumps({"type": "toggle", "what": "mic"}))
        assert json.loads(ws.receive_text())["mic"] is True
        ws.send_text(json.dumps({"type": "utterance", "dry": True,
                                 "wav": base64.b64encode(b"RIFFwav").decode()}))
        heard = json.loads(ws.receive_text())
        assert heard == {"type": "heard", "text": "what is pending", "did": "dry"}
        ws.send_text(json.dumps({"type": "locked"}))
        assert json.loads(ws.receive_text())["mic"] is False, "screen locked: mic off"
    assert vm.state()["helper"] is False
    vm._STATE.update(speaker=False, mic=False, helper=None)


def test_the_menu_bar_helper_builds(tmp_path):
    import sys
    if sys.platform != "darwin" or not shutil.which("swiftc") or not shutil.which("zsh"):
        pytest.skip("the helper is a macOS app (AppKit, Carbon) — built on a Mac only")
    root = Path(main.__file__).resolve().parents[1]
    out = subprocess.run(["zsh", str(root / "deploy" / "voice" / "install.sh"), "--build"],
                         capture_output=True, text=True, timeout=600,
                         env={**__import__("os").environ, "ASTA_VOICE_APP": str(tmp_path / "AstaVoice.app")})
    assert out.returncode == 0, out.stderr[-800:]
    plist = (tmp_path / "AstaVoice.app" / "Contents" / "Info.plist").read_text()
    assert "NSMicrophoneUsageDescription" in plist and "<key>LSUIElement</key><true/>" in plist
    src = (root / "deploy" / "voice" / "AstaVoice.swift").read_text()
    assert "kVK_ANSI_A" in src and "kVK_ANSI_M" in src, "⌃⌥A and ⌃⌥M"
    assert "setVoiceProcessingEnabled(useVoiceProcessing)" in src, "echo cancelling on the input"
    assert "firstChannel" in src, "the 9-channel echo-cancelled input is reduced to its first channel"
    assert "reopening the mic without echo cancelling" in src, "and falls back when it cannot start"
    assert "AVSpeechSynthesizer" in src, "the Mac's voice when Asta's is down"


# --- back and forth without lag ---------------------------------------------------------

def test_a_question_is_acknowledged_within_a_second_with_audio_made_beforehand(helper, monkeypatch):
    monkeypatch.setattr(vm, "ACK_SECONDS", 0.05)
    vm._CACHE["Let me check."] = "UkVBRFk="

    async def dispatch(conv, text, sink, channel):
        async def work():
            await asyncio.sleep(0.2)
            await sink.send({"type": "delta", "text": "Nothing pending."})
        return asyncio.ensure_future(work())

    monkeypatch.setattr(main, "_dispatch", dispatch)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    run(vm.handle("what is pending with my PRs?"))
    first = [m for m in helper.sent if m.get("type") == "say"][0]
    assert first["text"] == "Let me check." and first["audio"] == "UkVBRFk=", "no synthesis wait"
    vm._CACHE.clear()


def test_the_first_sentence_is_said_while_the_rest_is_still_being_written(helper, monkeypatch):
    monkeypatch.setattr(vm, "ACK_SECONDS", 5)
    said_at: list[float] = []

    async def dispatch(conv, text, sink, channel):
        async def work():
            await sink.send({"type": "delta", "text": "Booking PR 1429 is still waiting on Vinish. "})
            await sink.send({"type": "delta", "text": "AP"})
            said_at.append(len(helper.said()))
            await asyncio.sleep(0.05)
            await sink.send({"type": "delta", "text": " PR 1252 is mergeable. And more detail here."})
        return asyncio.ensure_future(work())

    monkeypatch.setattr(main, "_dispatch", dispatch)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    run(vm.handle("status of my PRs?"))
    assert said_at == [1], "said before the answer was finished"
    assert helper.said() == ["Booking PR 1429 is still waiting on Vinish.",
                             "AP PR 1252 is mergeable. The details are in the chat."]


def test_talking_over_asta_drops_the_rest_of_that_answer(helper, monkeypatch):
    monkeypatch.setattr(vm, "ACK_SECONDS", 5)

    async def dispatch(conv, text, sink, channel):
        async def work():
            await sink.send({"type": "delta", "text": "Booking PR 1429 is still waiting on Vinish. "})
            await sink.send({"type": "delta", "text": "x"})
            vm._STATE["barged_at"] = time.time() + 1          # he started talking
            await sink.send({"type": "delta", "text": " AP PR 1252 is mergeable."})
        return asyncio.ensure_future(work())

    monkeypatch.setattr(main, "_dispatch", dispatch)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time(), barged_at=0.0)
    run(vm.handle("status of my PRs?"))
    assert helper.said() == ["Booking PR 1429 is still waiting on Vinish."]


def test_a_comment_while_asta_works_is_folded_into_that_work(helper, monkeypatch):
    """The dispatcher already does this for WhatsApp; voice goes through it."""
    seen: list[str] = []

    async def dispatch(conv, text, sink, channel):
        seen.append(text)
        await sink.send({"type": "note", "text": "✚ adding that to what I'm doing — same task, no restart."})
        return None

    monkeypatch.setattr(main, "_dispatch", dispatch)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    run(vm.handle("also check UAT"))
    assert seen == ["also check UAT"]
    assert helper.said() == ["adding that to what I'm doing — same task, no restart."]


def test_acks_are_prepared_when_the_voice_comes_on(helper, monkeypatch):
    vm._CACHE.clear()
    run(vm.warm_acks())
    assert set(vm._CACHE) == set(vm.ACKS.values())
    vm._CACHE.clear()


def test_the_helper_interrupts_and_queues_lines():
    src = (Path(main.__file__).resolve().parents[1] / "deploy" / "voice" / "AstaVoice.swift").read_text()
    assert "func interrupt()" in src and "waiting.removeAll()" in src
    assert "onBargeIn" in src and '"type": "barge"' in src


def test_a_voice_turn_answers_even_when_no_brain_can_be_chosen(helper, monkeypatch):
    """CI, 2 Oct: no Claude CLI on the runner, so picking the brain raised and the
    turn died. The conversation's own model carries on."""
    def no_pick(conv):
        raise RuntimeError("tests must not reach a hosted model")

    async def dispatch(conv, text, sink, channel):
        await sink.send({"type": "delta", "text": "Nothing pending right now."})
        return None

    monkeypatch.setattr(main, "_channel_model", no_pick)
    monkeypatch.setattr(main, "_dispatch", dispatch)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    assert run(vm.handle("what is pending?"))["did"] == "answered"
    assert helper.said() == ["Nothing pending right now."]


@pytest.mark.parametrize("heard", ["موسيقى موسيقى موسيقى موسيقى", "[Music]", "♪ la la la ♪",
                                   "okay okay okay okay", "谢谢大家"])
def test_music_and_other_rooms_noise_are_not_turns(heard):
    assert vm.is_noise(heard)


@pytest.mark.parametrize("heard", ["मेरे pull request का क्या हाल है?", "Asta, check UAT please",
                                   "booking PR 1429 status"])
def test_his_english_and_hindi_still_are(heard):
    assert not vm.is_noise(heard)


# --- the talker in front, the worker behind ----------------------------------------------

@pytest.fixture
def talker(monkeypatch):
    """A scripted talker: each call to `sentences` plays the next reply."""
    from app import voice_talker
    replies: list[list[str]] = []
    asked: list[tuple] = []

    async def sentences(text, kind="said", timeout=30):
        asked.append((text, kind))
        for line in (replies.pop(0) if replies else []):
            yield line

    monkeypatch.setattr(voice_talker, "sentences", sentences)
    return replies, asked


def test_casual_words_and_the_room_get_silence(helper, talker):
    replies, _ = talker
    replies.append([QUIET := "[QUIET]"])
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    assert run(vm.handle("haha okay cool, I'll send you the deck after lunch"))["did"] == "not_for_asta"
    assert helper.said() == [] and QUIET


def test_a_status_question_is_answered_by_the_talker_without_the_worker(helper, talker, monkeypatch):
    async def dispatch(*a, **k):
        raise AssertionError("the worker was not needed")

    monkeypatch.setattr(main, "_dispatch", dispatch)
    replies, _ = talker
    replies.append(["Booking PR 1429 — the RFP mandatory validations one.",
                    "You've a message queued to Vinish Monday 9am."])
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    assert run(vm.handle("which of my PRs is waiting on Vinish?"))["did"] == "answered"
    assert helper.said() == ["Booking PR 1429 — the RFP mandatory validations one.",
                             "You've a message queued to Vinish Monday 9am."]


def test_work_is_acknowledged_once_and_the_outcome_said_once(helper, talker, monkeypatch):
    replies, asked = talker
    replies.append(["Checking now.", "[DO]"])
    dispatched: list[str] = []

    async def dispatch(conv, text, sink, channel):
        dispatched.append(text)
        await sink.send({"type": "delta", "text": "Checked Loki in UAT for H69LMCN6KZY: no hits in 72h."})
        return None

    monkeypatch.setattr(main, "_dispatch", dispatch)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())

    async def go():
        out = await vm.handle("check if booking H69LMCN6KZY reached UAT")
        for _ in range(20):
            await asyncio.sleep(0.01)
        return out

    assert run(go())["did"] == "handed_on"
    assert dispatched == ["check if booking H69LMCN6KZY reached UAT"], "his words, unchanged, to the worker"
    assert helper.said() == ["Checking now.", "Checked Loki in UAT for H69LMCN6KZY: no hits in 72h."], \
        "the worker's own words, no second pass through Claude"


def test_talking_over_the_talker_stops_it(helper, talker):
    replies, _ = talker
    replies.append(["Booking PR 1429 is waiting on Vinish.", "AP PR 1252 is mergeable."])
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time(), barged_at=time.time() + 5)
    run(vm.handle("status of my PRs?"))
    assert helper.said() == []


def test_without_a_talker_the_old_path_still_answers(helper, monkeypatch):
    from app import voice_talker

    async def none(text, kind="said", timeout=30):
        raise RuntimeError("no talker")
        yield ""                                                # pragma: no cover

    async def dispatch(conv, text, sink, channel):
        await sink.send({"type": "delta", "text": "Nothing pending."})
        return None

    monkeypatch.setattr(voice_talker, "sentences", none)
    monkeypatch.setattr(main, "_dispatch", dispatch)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    assert run(vm.handle("what is pending?"))["did"] == "answered"
    assert helper.said() == ["Nothing pending."]


def test_the_talker_reads_every_reply_to_its_end(monkeypatch):
    """Stopping at [QUIET] once left the rest in the pipe; the next question got it."""
    from app import call_mind, voice_talker

    class Mind:
        def __init__(self):
            self.proc = type("P", (), {"returncode": None})()
            self.reads = 0

        async def _stream(self, message, timeout):
            for piece in ["[QUIET]", " (and some", " trailing text)"]:
                self.reads += 1
                yield piece
            yield call_mind._COMPLETE

    m = Mind()
    voice_talker._TALKER.update(mind=m, briefed_at=time.time())

    async def go():
        got = [s async for s in voice_talker.sentences("yeah")]
        await asyncio.sleep(0.01)
        return got

    assert run(go()) == ["[QUIET]"]
    assert m.reads == 3, "the whole reply was consumed, so the next answer starts clean"
    voice_talker._TALKER.update(mind=None)


def test_the_talker_brief_keeps_its_promises():
    from app import voice_talker
    p = voice_talker.PERSONA
    assert "[QUIET]" in p and "[DO]" in p and "[RESULT]" in p
    for phrase in ("On it.", "Checking now.", "Let me look."):
        assert phrase in p and phrase in vm.ACKS.values(), "acknowledgements are pre-recorded"


# --- the instant local decision -----------------------------------------------------------

@pytest.fixture
def decide(monkeypatch):
    from app import voice_talker
    box = {"next": None}

    async def route(text):
        return box["next"]

    monkeypatch.setattr(voice_talker, "route", route)
    return box


def test_quiet_decided_locally_never_reaches_claude(helper, decide, talker):
    from app import voice_talker
    decide["next"] = voice_talker.QUIET
    replies, asked = talker
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    assert run(vm.handle("one sec, I'm on a call"))["did"] == "not_for_asta"
    assert asked == [] and helper.said() == []


def test_being_called_is_answered_at_once(helper, decide):
    from app import voice_talker
    decide["next"] = voice_talker.LISTEN
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    assert run(vm.handle("Asta, listen to me"))["did"] == "listening"
    assert helper.said() == ["I'm listening."]


def test_work_decided_locally_starts_one_job_and_a_repeat_starts_none(helper, decide, monkeypatch):
    from app import voice_talker
    decide["next"] = voice_talker.DO
    started: list[str] = []

    async def dispatch(conv, text, sink, channel):
        started.append(text)

        async def work():
            await asyncio.sleep(0.3)
            await sink.send({"type": "delta", "text": "done"})
        return asyncio.ensure_future(work())

    async def no_result(*a, **k):
        if False:
            yield ""

    monkeypatch.setattr(main, "_dispatch", dispatch)
    monkeypatch.setattr(voice_talker, "sentences", no_result)
    vm._JOBS.clear()
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())

    async def go():
        a = await vm.handle("check if booking H69LMCN6KZY reached UAT")
        await asyncio.sleep(0.05)
        b = await vm.handle("check booking H69LMCN6KZY reached UAT")
        await asyncio.sleep(0.5)
        return a, b

    a, b = run(go())
    assert a["did"] == "handed_on" and b["did"] == "already_on_it"
    assert started == ["check if booking H69LMCN6KZY reached UAT"]
    assert helper.said()[:2] == ["On it.", "Still on that — I'll tell you."]
    vm._JOBS.clear()


def test_two_different_jobs_run_side_by_side(helper, decide, monkeypatch):
    from app import voice_talker
    decide["next"] = voice_talker.DO
    running_now: list[int] = []
    peak = {"n": 0}

    async def dispatch(conv, text, sink, channel):
        async def work():
            running_now.append(1)
            peak["n"] = max(peak["n"], len(running_now))
            await asyncio.sleep(0.2)
            running_now.pop()
            await sink.send({"type": "delta", "text": f"result for {text}"})
        return asyncio.ensure_future(work())

    async def no_result(*a, **k):
        if False:
            yield ""

    monkeypatch.setattr(main, "_dispatch", dispatch)
    monkeypatch.setattr(voice_talker, "sentences", no_result)
    vm._JOBS.clear()
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())

    async def go():
        await vm.handle("check H69LMCN6KZY in UAT")
        await vm.handle("draft a reminder to Vinish about booking PR 1429")
        await asyncio.sleep(0.5)

    run(go())
    assert peak["n"] == 2, "the second did not wait for the first"
    assert len({j["cid"] for j in vm._JOBS.values()}) == 2, "each in its own conversation"
    vm._JOBS.clear()


def test_a_question_gets_one_ready_line_then_claudes_answer(helper, decide, talker, monkeypatch):
    from app import frontdesk, voice_talker
    decide["next"] = voice_talker.ANSWER
    monkeypatch.setattr(frontdesk, "answer_from_state", lambda text: None)
    replies, asked = talker
    replies.append(["Just booking PR 1429."])
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    run(vm.handle("which of my PRs is waiting on Vinish?"))
    assert asked and helper.said() == ["Let me see.", "Just booking PR 1429."]


def test_what_asta_already_knows_is_answered_without_a_brain(helper, decide, talker, monkeypatch):
    from app import frontdesk, voice_talker
    decide["next"] = voice_talker.ANSWER
    monkeypatch.setattr(frontdesk, "answer_from_state",
                        lambda text: "Waiting on you:\n• #201 Fix Contract Test failure on email PR 675 — plan")
    replies, asked = talker
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time())
    assert run(vm.handle("what's pending"))["did"] == "answered_from_state"
    assert asked == [] and helper.said() and "201" in helper.said()[0]


def test_the_same_request_in_other_words_is_still_the_same():
    assert vm.same_request("check if booking H69LMCN6KZY reached UAT",
                           "Asta check booking H69LMCN6KZY in UAT again")
    assert not vm.same_request("check booking H69LMCN6KZY in UAT",
                               "check booking H69LMCN6KZY in pre-prod")


def test_his_prs_are_answered_from_what_asta_holds(monkeypatch):
    from app import prname, reminders
    monkeypatch.setattr(prname, "his_open_prs", lambda limit=15: (
        "• booking PR 1429 — feat: fail RFP on missing mandatory downstream fields\n"
        "• AP PR 1252 — Derive ATA/ATD order-level references from TMS execution events"))
    reminders.schedule_send("Vinish Kumar", "bro can u merge these", time.time() + 3600)
    said = vm.pr_answer("which of my PRs is waiting on Vinish?")
    assert said.startswith("Your message to Vinish about them goes out"), "the part he asked about first"
    assert "You have 2 open PRs." in said and "booking PR 1429" in said
    assert vm.pr_answer("what's pending with my PRs?").startswith("You have 2 open PRs.")
    assert vm.pr_answer("check booking H69 in UAT") == ""
