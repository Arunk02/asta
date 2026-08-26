"""A call from the moment it is dialled to the moment it is reported.

Everything here failed silently in production before it was written, which is the
common thread and the reason the assertions are shaped the way they are:

  - A clicked call button was treated as a ringing phone that somebody answered.
  - A placed call had no watcher at all, so an unanswered one held the browser
    context — and blocked the NEXT call — until the server was restarted.
  - Speech was the only thing gated on whether Arun wanted it, so nothing stopped
    Asta talking over him in his own conversation.

None of those raised. Each one just made Asta confidently wrong in front of
somebody, which is why "returned successfully" is never what these check.
"""

from __future__ import annotations

import asyncio
import io
import math
import wave

import pytest

from app import meetings, notify, store, voice


def _wav(seconds: float = 0.05, rate: int = 24000) -> bytes:
    frames = bytearray()
    for i in range(int(rate * seconds)):
        frames += int(9000 * math.sin(2 * math.pi * 220 * i / rate)).to_bytes(
            2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class FakePage:
    """A Teams call screen, described by what is on it rather than by a mock.

    `body` is what the page says; `connected_marker` is whether the duration
    element exists; `timer` is whether a bare m:ss element is rendered. Between
    them they cover every state `call_state` has to tell apart.
    """

    def __init__(self, body: str = "", connected_marker: bool = False, timer: bool = False):
        self.body = body
        self.connected_marker = connected_marker
        self.timer = timer

    async def evaluate(self, js, *args):
        if "innerText" in js and "document.body" in js:
            return self.body
        return self.timer                       # the m:ss timer probe

    async def query_selector(self, sel):
        return object() if self.connected_marker else None


RINGING = "Vinish\nRinging…\nCancel"
CONNECTED = "Vinish\nYou are connected"
ENDED = "Call ended\nRejoin"


@pytest.fixture
def call(monkeypatch):
    """A placed call, mid-flight, with speaking possible on this machine."""
    monkeypatch.setattr(meetings, "can_speak", lambda: True)
    monkeypatch.setattr(meetings, "AUDIO_DEVICE", "BlackHole 2ch")
    monkeypatch.setattr(meetings, "HIS_MIC", "MacBook Pro Microphone")
    store.kv_set("teams_in_call", "call:Vinish")
    # By default the confirming model agrees it is a code question, which is the
    # ordinary case these tests describe. Speaking now needs that second opinion —
    # a regex is enough to put something on his phone and not enough to say it out
    # loud — so the tests about the gate itself override this.
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete", lambda prompt, n=8: "CODE")
    meetings._CALL.update(page=FakePage(RINGING), captions=[], answered_at=0.0,
                          speaks=True, who="Vinish",
                          joined_at=meetings._now())
    yield meetings._CALL
    store.kv_set("teams_in_call", "")
    meetings._CALL.clear()
    meetings._LAST_CALL.clear()
    meetings.clear_noticed()


@pytest.fixture
def spoke(monkeypatch):
    """Everything Asta actually said out loud, in order."""
    said: list[str] = []

    async def gen(text, profile="", engine="", voice=""):
        return _wav()

    async def mic(page, device):
        return True

    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(meetings, "set_call_mic", mic)
    monkeypatch.setattr(voice, "play_to_device",
                        lambda w, d="": said.append(w) or 0.05)
    monkeypatch.setattr(meetings, "_spoken_log", said, raising=False)
    return said


@pytest.fixture
def pushed(monkeypatch):
    """Everything that went to his phone instead."""
    out: list[str] = []

    async def spy(text, level="info", urgency="direct", priority=None):
        out.append(text)

    monkeypatch.setattr(notify, "notify", spy)
    return out


# --- ringing is not answered --------------------------------------------------
#
# The original bug in one sentence: `[data-tid="calling-hangup-button"]` is on the
# screen the instant Teams starts dialling, so waiting for it proved only that a
# button had been clicked. Asta then spoke into a phone nobody had picked up and
# reported that it had said its piece.

@pytest.mark.asyncio
async def test_a_ringing_phone_is_not_a_connected_call(call):
    assert await meetings.call_state(FakePage(RINGING)) == "ringing"


@pytest.mark.asyncio
async def test_a_caption_proves_the_call_connected(call):
    """The load-bearing evidence. Nobody talks into a phone that is still ringing,
    so a caption existing means somebody picked up — and unlike the duration
    selectors, the caption reader is proven against live Teams."""
    call["captions"] = [{"speaker": "Vinish", "text": "hey, what's up"}]
    assert await meetings.call_state(FakePage(RINGING)) == "connected"


@pytest.mark.asyncio
async def test_the_duration_marker_also_proves_it(call):
    assert await meetings.call_state(FakePage(CONNECTED, connected_marker=True)) == "connected"


@pytest.mark.asyncio
async def test_a_bare_timer_also_proves_it(call):
    assert await meetings.call_state(FakePage(CONNECTED, timer=True)) == "connected"


@pytest.mark.asyncio
async def test_an_ended_call_is_ended(call):
    assert await meetings.call_state(FakePage(ENDED)) == "ended"


@pytest.mark.asyncio
async def test_an_unreadable_screen_admits_it(call):
    """'unknown' exists so that a Teams restyle degrades into slowness rather
    than into Asta talking to a phone that is still ringing."""
    assert await meetings.call_state(FakePage("something entirely new")) == "unknown"


@pytest.mark.asyncio
async def test_a_dead_page_counts_as_ended(call):
    class Gone:
        async def evaluate(self, js, *a):
            raise RuntimeError("Target closed")

    assert await meetings.call_state(Gone()) == "ended"


@pytest.mark.asyncio
async def test_nothing_is_said_into_a_ringing_phone(call, spoke):
    with pytest.raises(RuntimeError, match="nobody has picked up"):
        await meetings.say_in_call("tell him the build passed")
    assert spoke == [], "it spoke into a call nobody had answered"


@pytest.mark.asyncio
async def test_speaking_is_allowed_once_somebody_answers(call, spoke):
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    await meetings.say_in_call("the build passed")
    assert len(spoke) == 1
    assert call["answered_at"] > 0, "answering was noticed but never recorded"


# --- waiting for the pickup ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_pickup_is_noticed(call):
    assert await meetings.wait_for_answer(
        FakePage(CONNECTED, connected_marker=True), seconds=2) == "connected"


@pytest.mark.asyncio
async def test_a_phone_still_ringing_at_the_deadline_is_no_answer(call):
    assert await meetings.wait_for_answer(FakePage(RINGING), seconds=2) == "no answer"


@pytest.mark.asyncio
async def test_an_unreadable_call_is_never_called_no_answer(call):
    """THE asymmetry. Hanging up on a conversation that is actually happening is
    far worse than sitting on one that is not, so absence of evidence must not
    become evidence of absence."""
    assert await meetings.wait_for_answer(FakePage("who knows"), seconds=2) == "unknown"


# --- how long it lasted -------------------------------------------------------

def test_an_unanswered_call_lasted_no_time_at_all(call):
    """Zero, not the 45 seconds it spent ringing. Ringing is not a conversation
    and reporting it as one puts a call in his day that never happened."""
    assert meetings.call_duration() == 0.0


@pytest.mark.asyncio
async def test_duration_runs_from_the_pickup(call):
    call["answered_at"] = meetings._now() - 90
    assert 89 <= meetings.call_duration() <= 92


@pytest.mark.parametrize("seconds,said", [
    (8, "8s"), (60, "1m"), (95, "1m 35s"), (600, "10m")])
def test_durations_are_said_the_way_he_would_say_them(seconds, said):
    assert meetings.spoken_duration(seconds) == said


@pytest.mark.asyncio
async def test_hanging_up_keeps_the_duration(call):
    """`leave()` clears the call, and how long it ran is wanted precisely after
    the hang-up — so it has to be read before the dict is emptied."""
    call["answered_at"] = meetings._now() - 30
    await meetings.leave()
    ended = meetings.last_call()
    assert ended["who"] == "Vinish"
    assert ended["answered"] is True
    assert 29 <= ended["seconds"] <= 32


# --- the call ends by itself --------------------------------------------------

@pytest.mark.asyncio
async def test_a_call_nobody_answers_is_hung_up_and_reported(call, pushed, monkeypatch):
    monkeypatch.setattr(meetings, "RING_SECONDS", 1.5)
    call["page"] = FakePage(RINGING)
    await meetings.call_watch("Vinish")
    assert store.kv_get("teams_in_call") in ("", None), "the call was never cleared"
    assert not meetings._CALL, "the call handle leaked — the next call would be refused"
    assert any("didn't pick up" in p for p in pushed)


@pytest.mark.asyncio
async def test_an_unreadable_call_is_kept_but_silenced(call, pushed, monkeypatch):
    """Stay on it — it may be a real conversation — but say nothing, because it
    may equally be a phone still ringing."""
    monkeypatch.setattr(meetings, "RING_SECONDS", 1.5)

    async def stop(title=""):
        return None

    monkeypatch.setattr(meetings, "watch_and_report", stop)
    call["page"] = FakePage("unrecognised screen")
    await meetings.call_watch("Vinish")
    assert meetings._CALL, "it hung up on a call it could not read"
    assert meetings.may_speak() is False, "it would have spoken into an unknown call"
    assert any("can't tell" in p for p in pushed)


@pytest.mark.asyncio
async def test_a_placed_call_gets_a_watcher(monkeypatch):
    """The hole this whole suite exists for. `join` has always spawned a watcher;
    `_teams_call` never did, so a placed call was never ended or reported."""
    from app import ops
    watched = []

    async def fake_call(who, video=False):
        return who

    async def fake_watch(title=""):
        watched.append(title)

    monkeypatch.setattr(meetings, "call_person", fake_call)
    monkeypatch.setattr(meetings, "call_watch", fake_watch)
    await ops._teams_call(who="Vinish")
    await asyncio.sleep(0.05)               # the watcher is spawned, not awaited
    assert watched == ["Vinish"], "nothing was watching the call"


# --- his conversation, his voice ----------------------------------------------

def test_hearing_him_closes_astas_mouth(call):
    assert meetings.may_speak() is True
    meetings._note_speaker("Arun K")
    assert meetings.may_speak() is False


def test_hearing_somebody_else_changes_nothing(call):
    meetings._note_speaker("Vinish")
    assert meetings.may_speak() is True


def test_the_silence_never_lifts_mid_call(call):
    """A one-way latch. Anything that turned speech back on would be a way for
    Asta to interrupt him; the call ending is what clears it."""
    meetings._note_speaker("Arun")
    meetings._note_speaker("Vinish")
    meetings._note_speaker("someone")
    assert meetings.may_speak() is False


def test_a_caption_from_him_is_what_triggers_it(call):
    meetings._merge_caption(call["captions"], "Vinish", "hey")
    meetings._note_speaker("Arun Kumar")
    assert meetings.may_speak() is False


# --- answering, or not --------------------------------------------------------

@pytest.fixture
def brain(monkeypatch):
    """A workspace that answers, with control over how slowly."""
    state = {"answer": "The ATA fallback picks the transport order in TmsServiceImpl.",
             "delay": 0.0}

    async def think(question):
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        return state["answer"]

    monkeypatch.setattr(meetings, "answer_from_knowledge", think)
    return state


ASK = {"kind": "answerable", "key": "k",
       "line": "how does the ATA fallback pick the transport order"}
HIS = {"kind": "his", "key": "h", "line": "can you review the PR today"}


@pytest.mark.asyncio
async def test_a_code_question_is_answered_out_loud_when_asta_is_alone(
        call, spoke, pushed, brain):
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    assert await meetings.handle_ask(ASK) == "spoken"
    assert len(spoke) == 2, "expected the holding line, then the answer"


@pytest.mark.asyncio
async def test_the_holding_line_comes_first(call, spoke, pushed, brain, monkeypatch):
    """It is the only part that can be fast. Thinking takes ten to thirty
    seconds; a cached acknowledgement takes four tenths of one, and it is the
    difference between a natural pause and dead air on the line."""
    order = []
    real = meetings.synth

    async def watched(text, voice_name=""):
        order.append(text)
        return await real(text, voice_name)

    monkeypatch.setattr(meetings, "synth", watched)
    brain["delay"] = 0.2
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    await meetings.handle_ask(ASK)
    assert order[0].startswith("Let me check"), f"the answer went first: {order}"


@pytest.mark.asyncio
async def test_warming_up_caches_the_words_that_are_actually_said(call, monkeypatch):
    """Warming has to go through the same transformation the speaking path does.
    `say_in_call` strips a trailing instruction and its punctuation, so warming
    the raw line caches it under text nobody ever asks for — the cache looks
    full, every lookup misses, and the eight seconds are paid anyway."""
    calls = []

    async def gen(text, profile="", engine="", voice=""):
        calls.append(text)
        return _wav()

    async def mic(page, device):
        return True

    monkeypatch.setattr(voice, "speak", gen)
    monkeypatch.setattr(meetings, "set_call_mic", mic)
    monkeypatch.setattr(voice, "play_to_device", lambda w, d="": 0.05)
    call["page"] = FakePage(CONNECTED, connected_marker=True)

    meetings.warm_the_voice()
    await asyncio.sleep(0.05)
    warmed = len(calls)
    assert warmed == len(meetings.HOLDING_LINES)

    await meetings.say_in_call(meetings.HOLDING_LINES["checking"])
    assert len(calls) == warmed, "the warmed line was synthesised again anyway"


@pytest.mark.asyncio
async def test_nothing_is_said_while_he_is_talking_but_the_work_still_happens(
        call, spoke, pushed, brain):
    """His actual instruction: while he is speaking to people Asta does not talk,
    but it still does the work and sends him what he needs."""
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    meetings._note_speaker("Arun")
    assert await meetings.handle_ask(ASK) == "sent to him"
    assert spoke == [], "it talked over him"
    assert any(brain["answer"] in p for p in pushed), "it went quiet AND did nothing"


@pytest.mark.asyncio
async def test_an_answer_that_took_too_long_goes_to_his_phone(
        call, spoke, pushed, brain, monkeypatch):
    monkeypatch.setattr(meetings, "ANSWER_BUDGET_SECONDS", 0.05)
    brain["delay"] = 0.2
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    assert await meetings.handle_ask(ASK) == "sent to him"
    assert len(spoke) == 1, "only the holding line should have been said"
    assert any("too long" in p for p in pushed)


@pytest.mark.asyncio
async def test_he_starts_talking_while_asta_is_thinking(
        call, spoke, pushed, brain, monkeypatch):
    """Twenty seconds is long enough for him to pick the conversation up himself.
    The answer to that is silence, not a sentence over the top of him."""
    async def think(question):
        meetings._note_speaker("Arun")
        return brain["answer"]

    monkeypatch.setattr(meetings, "answer_from_knowledge", think)
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    assert await meetings.handle_ask(ASK) == "sent to him"
    assert len(spoke) == 1, "it finished the thought out loud after he took over"


@pytest.mark.asyncio
async def test_a_question_for_arun_is_never_answered_for_him(
        call, spoke, pushed, brain):
    """Review it, merge it, when can you deploy — answering these commits him to
    things he never agreed to, whether or not Asta is alone on the call."""
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    assert await meetings.handle_ask(HIS) == "held"
    assert spoke and meetings.HOLDING_LINES["review"] not in brain["answer"]
    assert any("for you" in p for p in pushed)


@pytest.mark.asyncio
async def test_the_holding_line_promises_only_to_come_back(call):
    """What he asked for, word for word: give me a few minutes, I'll check and
    come back. It commits him to nothing."""
    line = meetings.HOLDING_LINES["review"].lower()
    assert "few minutes" in line and "come back" in line
    assert "no issues" not in line and "yes" not in line


@pytest.mark.asyncio
async def test_small_talk_is_not_worth_a_brain(call, spoke, pushed, brain):
    assert await meetings.handle_ask({"kind": "chatter", "line": "yeah", "key": "c"}) == "ignored"
    assert spoke == [] and pushed == []


@pytest.mark.asyncio
async def test_when_no_brain_can_answer_it_asks_him_instead(
        call, spoke, pushed, monkeypatch):
    async def nothing(question):
        return ""

    monkeypatch.setattr(meetings, "answer_from_knowledge", nothing)
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    assert await meetings.handle_ask(ASK) == "offered"


# --- the laptop going to sleep mid-call ---------------------------------------
#
# A call cannot survive sleep — browser, audio and network all stop, and Teams
# drops the far end within seconds. What survives is Asta's BELIEF that it is
# still on the call, and that belief blocks every later call with "already in a
# call" until the server restarts.

@pytest.mark.asyncio
async def test_sleeping_through_a_call_ends_it_and_says_so(call, pushed):
    assert await meetings.drop_call_lost_to_sleep(3600) == "Vinish"
    assert store.kv_get("teams_in_call") in ("", None)
    assert not meetings._CALL, "the next call would be refused as 'already in a call'"
    assert any("slept" in p and "Vinish" in p for p in pushed)


@pytest.mark.asyncio
async def test_waking_up_without_a_call_says_nothing(pushed):
    store.kv_set("teams_in_call", "")
    meetings._CALL.clear()
    assert await meetings.drop_call_lost_to_sleep(3600) == ""
    assert pushed == [], "it announced a call that never happened"


@pytest.mark.asyncio
async def test_a_dead_browser_handle_does_not_stop_the_cleanup(call, pushed):
    """The Playwright connection is gone after a sleep, so closing it throws.
    Cleaning up must not depend on the corpse being cooperative."""
    class Dead:
        async def close(self): raise RuntimeError("Target closed")

    call.update(ctx=Dead(), pw=Dead())
    await meetings.drop_call_lost_to_sleep(3600)
    assert not meetings._CALL and store.kv_get("teams_in_call") in ("", None)


@pytest.mark.asyncio
async def test_the_wake_watcher_ends_a_slept_through_call(call, pushed, monkeypatch):
    """Wired into the wake loop, not just available to it — the bug was that
    wake.py had no idea calls existed."""
    from app import wake
    seen = []

    async def spy(gap):
        seen.append(gap)
        return "Vinish"

    monkeypatch.setattr(meetings, "drop_call_lost_to_sleep", spy)
    monkeypatch.setattr(wake, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(wake, "MIN_GAP_SECONDS", 0.0)
    monkeypatch.setattr(wake, "ANNOUNCE_AFTER_SECONDS", 10 ** 9)

    async def online(limit=0):
        return True

    monkeypatch.setattr(wake, "wait_for_network", online)
    task = asyncio.create_task(wake.watch_loop())
    await asyncio.sleep(0.2)
    task.cancel()
    assert seen, "the wake loop never told the call it had been slept through"


@pytest.mark.asyncio
async def test_nothing_is_said_into_a_call_that_already_died(call, spoke):
    """Every line is checked, not just the first. The far end hangs up, or the
    Mac sleeps and takes the browser with it, and the old code would happily play
    audio into a microphone nobody was listening to and report it as said."""
    call["answered_at"] = meetings._now() - 20
    call["page"] = FakePage(ENDED)
    with pytest.raises(RuntimeError, match="has ended"):
        await meetings.say_in_call("so as I was saying")
    assert spoke == []


# --- what the mid-call brain is allowed to touch ------------------------------

def test_the_mid_call_brain_cannot_change_anything():
    """A sentence overheard in a meeting must not be able to send a message,
    move a ticket or start a task. The guarantee is that the brain does not hold
    the tool at all, so this asserts on the toolset rather than on behaviour."""
    names = {t.__name__ for t in meetings._call_tools()}
    assert names == {"resolve_context", "read_workspace_file",
                     "list_services", "search_memory"}
    forbidden = ("send", "post", "comment", "transition", "call", "merge",
                 "delegate", "prepare", "ask_user", "remember")
    assert not [n for n in names if any(f in n for f in forbidden)]


# --- being quick enough to be worth saying ------------------------------------

@pytest.mark.asyncio
async def test_a_repeated_line_is_only_synthesised_once(monkeypatch):
    """Measured: 1.1s warm, 8.9s cold. The holding lines are said in most calls
    and never change, so synthesising them twice buys the same second twice."""
    calls = []

    async def gen(text, profile="", engine="", voice=""):
        calls.append(text)
        return _wav()

    monkeypatch.setattr(voice, "speak", gen)
    line = meetings.HOLDING_LINES["checking"]
    assert await meetings.synth(line) == await meetings.synth(line)
    assert len(calls) == 1, "it paid the synthesis cost twice for the same words"


@pytest.mark.asyncio
async def test_the_same_words_in_a_different_voice_are_not_confused(monkeypatch):
    seen = []

    async def gen(text, profile="", engine="", voice=""):
        seen.append(voice)
        return _wav() if voice != "mine" else _wav(0.06)

    monkeypatch.setattr(voice, "speak", gen)
    await meetings.synth("all merged", "assistant")
    await meetings.synth("all merged", "mine")
    assert seen == ["assistant", "mine"], "the cache served his clone from the assistant"


def test_a_spoken_answer_is_trimmed_to_something_listenable():
    long = " ".join(f"word{i}" for i in range(200))
    out = meetings.spoken_form(long)
    assert len(out.split()) <= meetings.SPOKEN_ANSWER_WORDS + 1


def test_a_short_answer_is_left_exactly_as_it_is():
    assert meetings.spoken_form("It's handled in TmsServiceImpl.") == \
        "It's handled in TmsServiceImpl."


def test_trimming_prefers_to_end_on_a_sentence():
    text = ("The fallback lives in TmsServiceImpl and runs on amend. " * 3
            + "Then a great deal more detail nobody asked to hear out loud " * 5)
    assert meetings.spoken_form(text).rstrip().endswith((".", "…"))


# --- reacting without stalling the captions -----------------------------------

@pytest.mark.asyncio
async def test_reacting_never_blocks_caption_polling(call, monkeypatch):
    """Handling one ask can take half a minute. Captions scroll out of their
    window in seconds, so a caption missed while thinking is gone for good."""
    started = asyncio.Event()

    async def slow(item):
        started.set()
        await asyncio.sleep(5)

    monkeypatch.setattr(meetings, "handle_ask", slow)
    lines = [{"speaker": "Vinish",
              "text": "how does the ATA fallback pick the transport order"}]
    meetings.react_to(lines)                 # returns immediately, or this hangs
    await asyncio.wait_for(started.wait(), timeout=1)
    for t in list(meetings._REACTING):
        t.cancel()


@pytest.mark.asyncio
async def test_his_own_words_are_not_treated_as_questions_to_answer(call, monkeypatch):
    handled = []
    monkeypatch.setattr(meetings, "handle_ask",
                        lambda item: handled.append(item) or asyncio.sleep(0))
    meetings.react_to([{"speaker": "Arun",
                        "text": "how does the ATA fallback pick the transport order"}])
    await asyncio.sleep(0.05)
    assert handled == [], "it offered to answer a question he asked out loud"


@pytest.mark.asyncio
async def test_two_answers_are_never_spoken_over_each_other(call, monkeypatch):
    """Captions arrive in bursts. Two questions in one poll must be answered one
    after the other, not simultaneously into the same microphone."""
    live = []
    peak = []

    async def one(item):
        live.append(1)
        peak.append(len(live))
        await asyncio.sleep(0.05)
        live.pop()

    monkeypatch.setattr(meetings, "handle_ask", one)
    meetings.react_to([
        {"speaker": "Vinish", "text": "how does the ATA fallback pick the order"},
        {"speaker": "Vinish", "text": "where is the vessel schedule sync handled"},
    ])
    await asyncio.sleep(0.3)
    assert peak and max(peak) == 1, "two answers were being spoken at once"


# --- 17. A higher bar for speaking than for telling him ----------------------
#
# The classifier is regexes, which is the right tool for deciding whether to put
# something on his phone — cheap, instant, and a false positive costs a glance.
# It is the wrong tool for deciding what to SAY out loud: an incorrect sentence
# in front of a colleague is a conversation he cannot take back.

@pytest.fixture
def local_says(monkeypatch):
    """Control what the local model answers when asked to confirm speech."""
    def setter(verdict):
        from app import memory
        monkeypatch.setattr(memory, "local_llm_complete",
                            lambda prompt, n=8: verdict)
    return setter


@pytest.mark.asyncio
async def test_a_code_question_confirmed_is_spoken(call, spoke, pushed, brain, local_says):
    local_says("CODE")
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    assert await meetings.handle_ask(ASK) == "spoken"


@pytest.mark.asyncio
async def test_a_request_of_him_that_the_regex_missed_is_not_spoken(
        call, spoke, pushed, brain, local_says):
    """THE point of the higher bar. "can you check how the amend flow handles
    that" and "how does the amend flow handle that" differ by three words and by
    who is being committed to an answer."""
    local_says("PERSON")
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    assert await meetings.handle_ask(ASK) == "sent to him"
    assert spoke == [], "it answered aloud for him"
    assert any(brain["answer"] in p for p in pushed), "and it did not tell him either"


@pytest.mark.asyncio
async def test_with_no_local_model_it_stays_quiet(call, spoke, pushed, brain, monkeypatch):
    """Silence is always safe in a conversation; an unnecessary sentence is not."""
    from app import memory
    def dead(prompt, n=8):
        raise RuntimeError("LM Studio is not running")
    monkeypatch.setattr(memory, "local_llm_complete", dead)
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    assert await meetings.handle_ask(ASK) == "sent to him"
    assert spoke == []


@pytest.mark.asyncio
async def test_an_empty_verdict_is_not_a_yes(local_says):
    local_says("")
    assert await meetings.confident("how does the ATA fallback work") is False


@pytest.mark.asyncio
async def test_the_holding_line_does_not_need_the_higher_bar(
        call, spoke, pushed, brain, monkeypatch):
    """It commits him to nothing — "give me a few minutes" is true whatever the
    question turns out to be."""
    from app import memory
    monkeypatch.setattr(memory, "local_llm_complete",
                        lambda p, n=8: (_ for _ in ()).throw(RuntimeError("down")))
    call["page"] = FakePage(CONNECTED, connected_marker=True)
    assert await meetings.handle_ask(HIS) == "held"
    assert spoke, "it went silent on a line that commits him to nothing"
