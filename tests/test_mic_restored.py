"""His microphone comes back, whatever the call did.

`join` and `call_person` both switch the SYSTEM default input to the virtual
device before Teams binds a track. Only `say_in_call` ever switched it back, in
a `finally` guarded by `if borrowed` — so the microphone was returned only on
calls where Asta actually spoke.

A call nobody answered, one that was declined, or a meeting Asta only listened
to therefore ended with the system input still on BlackHole. That is the failure
this module's own comment calls the worst it can produce: his next meeting has a
microphone that is live, correctly labelled, and carries nothing. Found on 8 Sep
after a test call to Ravi, with `self_test`'s docstring already recording it
happening twice in one day.
"""

from __future__ import annotations

import asyncio

from app import call_audio, meetings


def _quiet(monkeypatch, restored: list):
    """Patch where `_restore_mic` actually looks it up. The mic functions live in
    `call_audio` now; `meetings` re-exports them, and patching the re-export
    leaves the real one in place — which is exactly the seam that hides a broken
    tool behind a green test."""
    async def fake_set(page=None, device: str = ""):
        restored.append(device)
        return True
    monkeypatch.setattr(call_audio, "set_call_mic", fake_set)


def _resolved(value):
    """A coroutine already carrying `value` — for stubbing an async lookup."""
    async def go():
        return value
    return go()


class _Ctx:
    def __init__(self): self.closed = False
    async def close(self): self.closed = True


def test_hanging_up_gives_the_microphone_back(monkeypatch):
    restored: list = []
    _quiet(monkeypatch, restored)
    meetings._CALL.clear()
    meetings._CALL.update(who="Ravi Menon", ctx=_Ctx())

    asyncio.run(meetings.leave())
    assert restored == [meetings.HIS_MIC]


def test_it_comes_back_even_when_asta_never_spoke(monkeypatch):
    """The whole point. Nobody picked up, so `say_in_call` never ran — and that
    was the only thing restoring the mic."""
    restored: list = []
    _quiet(monkeypatch, restored)
    meetings._CALL.clear()
    meetings._CALL.update(who="Ravi Menon", ctx=_Ctx(), speaks=[], answered_at=None)

    asyncio.run(meetings.leave())
    assert meetings.HIS_MIC in restored


def test_it_comes_back_even_when_closing_the_browser_fails(monkeypatch):
    """A teardown that raises must not take the microphone with it."""
    restored: list = []
    _quiet(monkeypatch, restored)

    class _Bad:
        async def close(self): raise RuntimeError("browser already gone")
    meetings._CALL.clear()
    meetings._CALL.update(who="X", ctx=_Bad())

    asyncio.run(meetings.leave())
    assert meetings.HIS_MIC in restored


def test_leaving_when_not_in_a_call_touches_nothing(monkeypatch):
    """`leave` is called speculatively. Switching the input on a no-op would
    take the mic away from whatever he is actually doing."""
    restored: list = []
    _quiet(monkeypatch, restored)
    meetings._CALL.clear()

    assert asyncio.run(meetings.leave()) == "not in a call"
    assert restored == []


def test_a_failed_restore_is_shouted_about(monkeypatch):
    """Silently muted for the rest of the day is the outcome this prevents."""
    said: list = []

    async def fake_set(page=None, device: str = ""):
        return False
    monkeypatch.setattr(call_audio, "set_call_mic", fake_set)
    monkeypatch.setattr(call_audio, "current_mic",
                        lambda: _resolved("BlackHole 2ch"))

    from app import notify

    async def fake_notify(text, level="info", **kw):
        said.append(text)
        return {}
    monkeypatch.setattr(notify, "notify", fake_notify)

    asyncio.run(meetings._restore_mic(None))
    assert said and "mic" in said[0].lower()


def test_every_switch_to_the_virtual_device_has_a_way_back():
    """Pins the shape rather than one call site: anything that points the system
    input at the call device must be balanced by the teardown that returns it."""
    import inspect
    src = inspect.getsource(meetings)
    takes = src.count("set_call_mic(device=AUDIO_DEVICE)")
    assert takes >= 2, "join and call_person both borrow the mic"
    assert "call_audio._restore_mic(None)" in inspect.getsource(meetings.leave)


# --- a call nobody is on does not sit there ----------------------------------

def test_an_unanswered_call_is_hung_up_not_left_ringing(monkeypatch):
    """`wait_for_answer` returns "unknown" when it can read neither ringing nor
    connected, and unknown is deliberately left alone — hanging up on a call that
    IS happening is worse. But "left alone" had no ceiling: the test call to
    Ravi on 8 Sep sat from 11:02 until a speak attempt raised at 11:23."""
    meetings._CALL.clear()
    meetings._CALL.update(who="Ravi Menon",
                          joined_at=meetings._now() - meetings.UNANSWERED_SECONDS - 1,
                          captions=[], answered_at=0.0)
    assert asyncio.run(meetings._never_answered()) is True


def test_a_call_someone_picked_up_is_never_dropped():
    meetings._CALL.clear()
    meetings._CALL.update(joined_at=meetings._now() - 9999,
                          answered_at=meetings._now(), captions=[])
    assert asyncio.run(meetings._never_answered()) is False


def test_captions_alone_prove_somebody_is_there(monkeypatch):
    """The second signal, and the reason this is safe: `answered_at` is set only
    when the call screen reads "connected", so an unreadable screen would drop a
    live call. A caption only exists while somebody is speaking."""
    meetings._CALL.clear()
    meetings._CALL.update(joined_at=meetings._now() - 9999, answered_at=0.0,
                          captions=[{"who": "Ravi", "text": "hello?"}])
    assert asyncio.run(meetings._never_answered()) is False


def test_a_call_still_within_its_grace_period_is_left_alone():
    meetings._CALL.clear()
    meetings._CALL.update(joined_at=meetings._now() - 5, answered_at=0.0, captions=[])
    assert asyncio.run(meetings._never_answered()) is False


def test_no_call_is_not_an_unanswered_one():
    meetings._CALL.clear()
    assert asyncio.run(meetings._never_answered()) is False


def test_the_watch_loop_actually_checks_it():
    """`watch` is the loop a placed call sits in — the one that ran for 21
    minutes. A guard nothing calls is a guard that does not exist."""
    import inspect
    assert "_never_answered" in inspect.getsource(meetings.watch)


# --- she answered, and it hung up on her -------------------------------------

class _Page:
    """A Teams call screen that keeps its ringing chrome after the call connects
    — which is what Ravi's actually did on 8 Sep."""

    def __init__(self, text, audio=False):
        self.text, self.audio = text, audio

    async def evaluate(self, js):
        if "srcObject" in js:
            return self.audio
        if "innerText" in js:
            return self.text
        return False

    async def query_selector(self, sel):
        return None


def test_remote_audio_means_they_answered_even_while_it_still_says_ringing():
    """Confirmed by Arun watching it happen: "they accepted the call, they keep
    on calling for sometimes arun arun and then they ended the call" — while
    Asta reported "no answer. I said nothing and hung up."

    Every text detector said ringing for the whole 45s. A media element only
    plays once the far end is sending sound, so this is the one signal that does
    not depend on Teams' UI wording at all."""
    from app import call_screen
    assert asyncio.run(call_screen.call_state(_Page("Ringing…"))) == "ringing"
    assert asyncio.run(
        call_screen.call_state(_Page("Ringing…", audio=True))) == "connected"


def test_a_ringback_tone_is_not_mistaken_for_an_answer():
    """Teams plays ringback from a FILE through the same elements. The signal is
    `srcObject` — a live remote MediaStream, which WebRTC does not negotiate for
    a call still ringing — so a ringing page with no stream stays ringing."""
    from app import call_screen
    assert asyncio.run(
        call_screen.call_state(_Page("Calling Ravi Menon…"))) == "ringing"
    assert "srcObject" in call_screen._AUDIO_JS


def test_captions_still_outrank_everything():
    from app import call_screen
    page = _Page("Ringing…")
    assert asyncio.run(
        call_screen.call_state(page, captions=[{"text": "hello?"}])) == "connected"


def test_captions_are_polled_while_waiting_for_an_answer():
    """The reliable signal was structurally unavailable at the only moment it
    mattered: `call_state` weighs captions most heavily, and nothing collected
    any until `watch` started — which is after `wait_for_answer` has returned."""
    import inspect
    assert "poll_captions" in inspect.getsource(meetings.wait_for_answer)


# --- looking at the window the call is actually on ---------------------------

class _Ctx2:
    def __init__(self, pages): self.pages = pages


class _Win:
    def __init__(self, text="", chrome=False, closed=False):
        self.text, self.chrome, self._closed = text, chrome, closed

    def is_closed(self): return self._closed
    async def query_selector(self, sel): return object() if self.chrome else None
    async def evaluate(self, js): return self.text


def test_the_call_window_is_found_not_assumed():
    """Teams opens a call in its OWN window. Every reader was handed
    `ctx.pages[0]` — the main Teams page — and kept it, so once the call moved
    out every detector looked at a window with no call on it and honestly said
    "unknown" for ever. That is how the third call to Ravi ended: not one
    signal fired, because none were looking at the call."""
    from app import call_screen
    main, call = _Win("Chats  Teams  Calendar"), _Win(chrome=True)
    got = asyncio.run(call_screen.call_page(_Ctx2([main, call]), main))
    assert got is call


def test_the_newest_matching_window_wins():
    """A call window opened seconds ago beats a stale one from a previous try."""
    from app import call_screen
    old, new = _Win(chrome=True), _Win(chrome=True)
    assert asyncio.run(call_screen.call_page(_Ctx2([old, new]), None)) is new


def test_a_closed_window_is_skipped():
    from app import call_screen
    dead, live = _Win(chrome=True, closed=True), _Win("Ringing…")
    assert asyncio.run(call_screen.call_page(_Ctx2([dead, live]), None)) is live


def test_with_no_call_window_the_caller_keeps_its_page():
    from app import call_screen
    main = _Win("Chats  Teams  Calendar")
    assert asyncio.run(call_screen.call_page(_Ctx2([main]), main)) is main


def test_an_unknown_state_records_what_the_screen_said():
    """"unknown" alone is unactionable — it says a reader failed without saying
    what it read, and three calls were spent guessing at it."""
    from app import call_screen
    seen = asyncio.run(call_screen.describe(_Win("Ringing…\nRavi Menon")))
    assert "Ravi Menon" in seen
    assert asyncio.run(call_screen.describe(None)) == "no page"


def test_the_failure_message_carries_it():
    import inspect
    assert "The call window showed" in inspect.getsource(meetings.say_in_call)


# --- the warning that cried wolf ---------------------------------------------

def test_a_switch_that_lands_late_is_not_reported_as_a_failure(monkeypatch):
    """`set_call_mic` verifies by reading the input straight after writing it,
    and that read RACES — Teams grabs the system input for itself while a call is
    up ("Microsoft Teams Audio" is a real device and wins). Arun got "you may be
    muted" again and again on calls where his mic was fine, which is worse than
    useless: the one time it is true, he has learned to ignore it."""
    said: list = []
    tries: list = []

    async def flaky(page=None, device: str = ""):
        tries.append(device)
        return len(tries) >= 3          # lands on the third go
    monkeypatch.setattr(call_audio, "set_call_mic", flaky)

    from app import notify

    async def fake_notify(text, level="info", **kw):
        said.append(text)
        return {}
    monkeypatch.setattr(notify, "notify", fake_notify)

    asyncio.run(call_audio._restore_mic(None))
    assert said == [], "it retried and succeeded — nothing to warn about"


def test_a_real_failure_says_which_device_it_is_actually_on(monkeypatch):
    """"you may be muted" alone gives him nothing to act on."""
    said: list = []

    async def never(page=None, device: str = ""):
        return False
    monkeypatch.setattr(call_audio, "set_call_mic", never)
    monkeypatch.setattr(call_audio, "current_mic",
                        lambda: _resolved("Microsoft Teams Audio"))
    from app import store, notify
    store.kv_set("mic_warned_for", "")

    async def fake_notify(text, level="info", **kw):
        said.append(text)
        return {}
    monkeypatch.setattr(notify, "notify", fake_notify)

    asyncio.run(call_audio._restore_mic(None))
    assert said and "Microsoft Teams Audio" in said[0]


def test_the_same_warning_is_not_repeated(monkeypatch):
    """Once per call, not once per attempt. Repeating it is what made it noise."""
    said: list = []

    async def never(page=None, device: str = ""):
        return False
    monkeypatch.setattr(call_audio, "set_call_mic", never)
    monkeypatch.setattr(call_audio, "current_mic",
                        lambda: _resolved("Microsoft Teams Audio"))
    from app import store, notify
    store.kv_set("mic_warned_for", "")

    async def fake_notify(text, level="info", **kw):
        said.append(text)
        return {}
    monkeypatch.setattr(notify, "notify", fake_notify)

    asyncio.run(call_audio._restore_mic(None))
    asyncio.run(call_audio._restore_mic(None))
    assert len(said) == 1


def test_an_unknown_device_warning_is_not_swallowed_by_a_previous_success(monkeypatch):
    """The marker used "" for BOTH "nothing outstanding" (set on success) and
    "the device could not be read". So after any successful restore, a genuine
    unknown-device failure compared equal to the marker and was silently
    dropped — the one time the warning is true, he would not hear it."""
    said: list = []

    async def never(page=None, device: str = ""):
        return False

    monkeypatch.setattr(call_audio, "set_call_mic", never)
    monkeypatch.setattr(call_audio, "current_mic", lambda: _resolved(""))
    from app import notify, store

    async def fake_notify(text, level="info", **kw):
        said.append(text)
        return {}

    monkeypatch.setattr(notify, "notify", fake_notify)
    store.kv_set("mic_warned_for", "")          # as a successful restore leaves it
    asyncio.run(call_audio._restore_mic(None))
    assert len(said) == 1 and "unknown device" in said[0]
    # ...and still only once, however many calls end the same way.
    asyncio.run(call_audio._restore_mic(None))
    assert len(said) == 1
