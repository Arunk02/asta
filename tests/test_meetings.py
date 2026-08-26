"""Invites, calls, and presence — the things other people see.

An invite books time in other people's calendars and a leave request goes to
whoever approves it, so both are built here and sent nowhere. The construction is
what these tests are mostly about, because it is the part that can be wrong in a
way nobody notices until the day itself: an off-by-one on a leave date is found by
the person who needed him on the day he was actually in.

The other half is refusing to pretend. Joining a call works; speaking in one needs
a virtual microphone the machine may not have, and the failure worth engineering
against is the quiet one — audio generated, played to a device nobody is listening
to, and reported as though his point was made.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from datetime import datetime

import pytest

from app import meetings, offers, store


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    offers.clear()
    yield


def _params(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


# --- building an invite -----------------------------------------------------

def test_a_meeting_carries_every_field_outlook_needs():
    inv = meetings.meeting_invite("Design sync", "2026-07-30 15:00", 45,
                                  ["priya@co.com", "sam@co.com"], "Schema options")
    q = _params(inv["url"])
    assert q["subject"] == ["Design sync"]
    assert q["startdt"] == ["2026-07-30T15:00:00"]
    assert q["enddt"] == ["2026-07-30T15:45:00"]
    assert q["to"] == ["priya@co.com;sam@co.com"]
    assert q["body"] == ["Schema options"]


def test_a_meeting_is_online_by_default():
    assert "online" in _params(meetings.meeting_invite("x", "2026-07-30 09:00")["url"])


def test_one_day_of_leave_covers_that_whole_day():
    """The calendar's end is exclusive, so a single day off has to run to the next
    morning. Getting this wrong shows him as present on the day he is away."""
    inv = meetings.leave_invite("2026-08-03")
    assert inv["start"] == datetime(2026, 8, 3)
    assert inv["end"] == datetime(2026, 8, 4)
    assert inv["days"] == 1
    assert _params(inv["url"])["allday"] == ["true"]


def test_a_leave_range_is_inclusive_at_both_ends():
    inv = meetings.leave_invite("2026-08-03", "2026-08-07")
    assert inv["days"] == 5
    assert inv["end"] == datetime(2026, 8, 8)


def test_leave_goes_to_whoever_approves_it():
    inv = meetings.leave_invite("2026-08-03", reason="family", to=["boss@co.com"])
    assert _params(inv["url"])["to"] == ["boss@co.com"]
    assert "family" in inv["subject"]


def test_leave_is_not_an_online_meeting():
    """A Teams link on an out-of-office invite invites people to join him on leave."""
    assert "online" not in _params(meetings.leave_invite("2026-08-03")["url"])


def test_backwards_leave_dates_are_refused():
    with pytest.raises(RuntimeError, match="before it starts"):
        meetings.leave_invite("2026-08-07", "2026-08-03")


@pytest.mark.parametrize("bad", ["thursday", "2026-13-01", "03/08/2026", ""])
def test_a_date_it_cannot_be_sure_of_is_refused(bad):
    """Deliberately not a natural-language parser. A library that disagrees with
    him about which Thursday books real time in other people's calendars."""
    with pytest.raises(RuntimeError):
        meetings.leave_invite(bad)


@pytest.mark.parametrize("bad", ["2026-07-30", "2026-07-30 25:00", "next tuesday 3pm"])
def test_a_meeting_time_it_cannot_be_sure_of_is_refused(bad):
    with pytest.raises(RuntimeError):
        meetings.meeting_invite("x", bad)


def test_what_he_reads_before_approving_states_the_facts():
    inv = meetings.meeting_invite("Design sync", "2026-07-30 15:00", 45, ["priya@co.com"])
    text = meetings.describe(inv)
    assert "Design sync" in text and "15:00" in text and "priya@co.com" in text


def test_a_leave_description_says_all_day_and_how_many():
    text = meetings.describe(meetings.leave_invite("2026-08-03", "2026-08-07"))
    assert "all day" in text and "5 days" in text


def test_an_invite_with_nobody_on_it_says_so_rather_than_looking_empty():
    assert "no attendees" in meetings.describe(meetings.meeting_invite("x", "2026-07-30 09:00"))


# --- nothing sends itself ---------------------------------------------------

def test_creating_a_meeting_stages_it_and_sends_nothing():
    from app import agent
    out = asyncio.run(agent.create_meeting("Design sync", "2026-07-30 15:00", 30,
                                           "priya@co.com"))
    o = offers.pending()
    assert "waiting for Arun" in out
    assert o and o.mechanical() and o.op["name"] == "calendar_send"
    assert "Design sync" in o.context


def test_requesting_leave_stages_it_and_sends_nothing():
    from app import agent
    asyncio.run(agent.request_leave("2026-08-03", "2026-08-07", "family", "boss@co.com"))
    o = offers.pending()
    assert o and o.op["name"] == "calendar_send"
    assert "5 days" in o.context


def test_a_bad_date_never_becomes_a_staged_invite():
    from app import agent
    out = asyncio.run(agent.create_meeting("x", "thursday afternoon"))
    assert "Can't build" in out
    assert offers.pending() is None


def test_the_staged_op_carries_the_built_url_not_a_description():
    from app import agent
    asyncio.run(agent.create_meeting("Design sync", "2026-07-30 15:00"))
    url = offers.pending().op["args"]["url"]
    assert _params(url)["subject"] == ["Design sync"]


def test_an_invite_that_did_not_send_is_reported_as_not_sent(monkeypatch):
    """open_and_send returning anything but 'sent' must not read as success."""
    from app import ops

    async def not_sent(url, send=False):
        return "opened (not sent)"

    monkeypatch.setattr(ops.meetings, "open_and_send", not_sent)
    with pytest.raises(RuntimeError, match="not sent"):
        asyncio.run(ops.run({"name": "calendar_send", "args": {"url": "u", "summary": "s"}}))


# --- calls ------------------------------------------------------------------

def test_speaking_is_refused_when_no_microphone_is_configured(monkeypatch):
    monkeypatch.setattr(meetings, "AUDIO_DEVICE", "")
    store.kv_set("teams_in_call", "https://teams/x")
    with pytest.raises(RuntimeError, match="virtual microphone"):
        asyncio.run(meetings.say_in_call("hello"))


def test_the_refusal_explains_what_would_make_it_work(monkeypatch):
    monkeypatch.setattr(meetings, "AUDIO_DEVICE", "")
    assert "ASTA_CALL_AUDIO_DEVICE" in meetings.speaking_hint()
    assert not meetings.can_speak()


def test_speaking_outside_a_call_is_refused(monkeypatch):
    monkeypatch.setattr(meetings, "AUDIO_DEVICE", "BlackHole 2ch")
    store.kv_set("teams_in_call", "")
    with pytest.raises(RuntimeError, match="not in a call"):
        asyncio.run(meetings.say_in_call("hello"))


def test_silent_speech_generation_is_not_reported_as_spoken(monkeypatch):
    """Audio that came back empty means nothing was said, whatever the pipeline
    thought it was doing."""
    monkeypatch.setattr(meetings, "AUDIO_DEVICE", "BlackHole 2ch")
    store.kv_set("teams_in_call", "https://teams/x")

    async def nothing(text, profile="", engine="", voice=""):
        return b""

    from app import voice
    monkeypatch.setattr(voice, "speak", nothing)
    with pytest.raises(RuntimeError, match="said nothing"):
        asyncio.run(meetings.say_in_call("hello"))


def test_the_tool_reports_the_reason_rather_than_raising(monkeypatch):
    """A tool that raises gives the model a stack trace to paraphrase; a tool that
    explains gives it something true to relay."""
    from app import agent
    monkeypatch.setattr(meetings, "AUDIO_DEVICE", "")
    out = asyncio.run(agent.say_in_call("hello"))
    assert out.startswith("Said nothing")


def test_joining_needs_a_real_link():
    with pytest.raises(RuntimeError, match="join link"):
        asyncio.run(meetings.join("the standup"))


def test_a_call_has_a_hard_ceiling():
    """A meeting that overruns — or a bug — must not leave Asta sitting in
    someone's call all day."""
    assert meetings.MAX_CALL_MINUTES > 0


# --- presence ---------------------------------------------------------------

@pytest.mark.parametrize("said,shown", [
    ("dnd", "Do not disturb"), ("do not disturb", "Do not disturb"),
    ("busy", "Busy"), ("available", "Available"), ("free", "Available"),
    ("brb", "Be right back"), ("away", "Appear away"), ("offline", "Appear offline"),
])
def test_the_words_he_uses_map_to_the_words_teams_shows(said, shown):
    from app import teams_bridge
    assert teams_bridge.presence_label(said) == shown


def test_an_unrecognised_status_is_refused_rather_than_guessed():
    """Silently setting Busy when he asked for something else makes him look
    available to nobody and unavailable to everybody, with no reason to check."""
    from app import teams_bridge
    with pytest.raises(RuntimeError, match="isn't a Teams status"):
        teams_bridge.presence_label("in the zone")


def test_the_refusal_lists_what_it_does_accept():
    from app import teams_bridge
    with pytest.raises(RuntimeError, match="Do not disturb"):
        teams_bridge.presence_label("nope")


def test_case_and_spacing_do_not_matter():
    from app import teams_bridge
    assert teams_bridge.presence_label("  Do   Not Disturb ") == "Do not disturb"


# --- staying in a call, and getting back out ---------------------------------
#
# The failure worth engineering against is not a call that ends badly, it is one
# that never ends: a restyled post-call screen, a marker that stops matching, and
# Asta parked in somebody's meeting for the rest of the day.

class _FakePage:
    def __init__(self, text=""):
        self.text = text

    async def evaluate(self, script):
        return self.text


def test_leaving_when_not_in_a_call_says_so():
    meetings._CALL.clear()
    assert asyncio.run(meetings.leave()) == "not in a call"


def test_hanging_up_closes_the_browser_and_clears_the_flag():
    """Closing the context IS leaving the call — a cleared flag with a live window
    would show him present in a meeting he thinks he left."""
    closed = {}

    class _Ctx:
        async def close(self):
            closed["ctx"] = True

    class _Pw:
        async def stop(self):
            closed["pw"] = True

    meetings._CALL.update(ctx=_Ctx(), pw=_Pw(), page=None, joined_at=0)
    store.kv_set("teams_in_call", "https://teams/x")
    assert asyncio.run(meetings.leave()) == "left the call"
    assert closed == {"ctx": True, "pw": True}
    assert not store.kv_get("teams_in_call")
    assert meetings._CALL == {}


def test_joining_twice_is_refused():
    meetings._CALL.update(url="https://teams/x")
    try:
        with pytest.raises(RuntimeError, match="already in a call"):
            asyncio.run(meetings.join("https://teams/y"))
    finally:
        meetings._CALL.clear()


@pytest.mark.parametrize("screen", ["Call ended", "You left the meeting", "Rejoin"])
def test_the_end_of_a_call_is_noticed(screen):
    assert asyncio.run(meetings.call_ended(_FakePage(screen)))


def test_a_live_call_is_not_mistaken_for_an_ended_one():
    assert not asyncio.run(meetings.call_ended(_FakePage("Priya is presenting")))


def test_a_dead_page_counts_as_ended():
    """The window is gone. Treating that as 'still in the call' would leave the
    watcher polling something that no longer exists, forever."""
    class _Dead:
        async def evaluate(self, script):
            raise RuntimeError("target closed")

    assert asyncio.run(meetings.call_ended(_Dead()))


def test_the_ceiling_is_what_saves_him_when_the_marker_stops_matching(monkeypatch):
    monkeypatch.setattr(meetings, "MAX_CALL_MINUTES", 30)
    loop = asyncio.new_event_loop()
    try:
        meetings._CALL.update(joined_at=loop.time(), page=None)
        assert not meetings.overran(now=loop.time() + 60 * 10)     # 10 min in
        assert meetings.overran(now=loop.time() + 60 * 31)         # past the ceiling
    finally:
        meetings._CALL.clear()
        loop.close()


def test_nothing_overruns_when_there_is_no_call():
    meetings._CALL.clear()
    assert not meetings.overran()


def test_watching_nothing_is_harmless():
    meetings._CALL.clear()
    assert asyncio.run(meetings.watch()) == "not in a call"


def test_the_watcher_leaves_once_the_call_ends():
    class _Ctx:
        async def close(self):
            pass

    class _Pw:
        async def stop(self):
            pass

    meetings._CALL.update(ctx=_Ctx(), pw=_Pw(), page=_FakePage("Call ended"),
                          joined_at=asyncio.new_event_loop().time())
    try:
        assert asyncio.run(meetings.watch(poll_seconds=0)) == "the call ended"
        assert meetings._CALL == {}
    finally:
        meetings._CALL.clear()
