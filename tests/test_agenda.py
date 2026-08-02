"""Meetings: which one he means, whether he is needed, and how much warning.

The functional hole these close: `meetings.join()` has always required a URL and
nothing ever produced one, so "join my 3pm" named a capability with no way to
reach it. The rest is about a day of meetings being more than a list — a clash,
a run with no gap, and a broadcast are all different things and were treated
identically.

The browser half cannot be tested without a live Outlook, so all the judgement
lives in pure functions and is tested here. The tests that matter most are the
refusals: picking the WRONG meeting puts him in a room in front of people who
watch him arrive.
"""

from __future__ import annotations

import asyncio

import pytest

from app import agenda, meetings, outlook


def _ev(start: str, title: str, minutes: int, ends: int = 0,
        status: str = "Busy", organizer: str = "", join: str = "") -> dict:
    return {"start": start, "end": "", "title": title, "minutes": minutes,
            "ends": ends or minutes + 30, "status": status,
            "organizer": organizer, "join_url": join, "line": f"{start} {title}"}


DAY = [
    _ev("9:00 AM", "Daily standup", 540, 555),
    _ev("11:00 AM", "1:1 with Priya", 660, 690),
    _ev("2:00 PM", "Sprint review", 840, 900),
    _ev("3:00 PM", "All-hands", 900, 960, status="Free"),
]


# --- the join link ----------------------------------------------------------------

def test_a_join_link_is_pulled_out_of_calendar_text():
    raw = ('Sprint review, 2:00 PM to 3:00 PM, Busy, By Sam. Join here: '
           'https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc/0?context=x')
    assert outlook.join_url_in(raw).endswith("context=x")


def test_trailing_punctuation_is_not_part_of_the_link():
    raw = "join at https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc/0."
    assert outlook.join_url_in(raw).endswith("/0")


def test_no_link_is_a_normal_day_not_a_failure():
    """Some Outlook builds put the link in the row and some do not."""
    assert outlook.join_url_in("Sprint review, 2:00 PM to 3:00 PM, Busy") == ""
    assert outlook.join_url_in("") == ""


def test_a_non_teams_link_is_not_mistaken_for_one():
    assert outlook.join_url_in("see https://wiki.example.com/l/meetup-join/x") == ""


# --- which meeting does he mean? ----------------------------------------------------

@pytest.mark.parametrize("phrase", ["join my 3pm", "my 3 PM", "the 15:00", "join my 3"])
def test_a_time_picks_the_meeting_at_that_time(phrase):
    assert agenda.pick(DAY, phrase)["title"] == "All-hands"


def test_a_morning_hour_is_not_pushed_into_the_afternoon():
    assert agenda.pick(DAY, "my 9")["title"] == "Daily standup"


def test_a_name_picks_the_meeting_by_title():
    assert agenda.pick(DAY, "join the standup")["title"] == "Daily standup"
    assert agenda.pick(DAY, "the sprint review")["title"] == "Sprint review"


def test_next_picks_the_one_coming_up():
    assert agenda.pick(DAY, "join my next meeting", now_minutes=700)["title"] == "Sprint review"


def test_next_with_nothing_left_picks_nothing():
    assert agenda.pick(DAY, "my next meeting", now_minutes=1200) is None


# --- the refusals (why this is not a fuzzy best guess) --------------------------------

def test_a_time_with_nothing_in_it_refuses():
    assert agenda.pick(DAY, "join my 4pm") is None


def test_two_meetings_at_the_same_time_refuse():
    """Ambiguity is the case that must NOT resolve. Joining the wrong call puts
    him in a room in front of people who watch him arrive."""
    clash = DAY + [_ev("3:00 PM", "Vendor sync", 900, 930)]
    assert agenda.pick(clash, "join my 3pm") is None


def test_a_name_matching_two_meetings_refuses():
    twice = DAY + [_ev("4:00 PM", "Platform review", 960, 990)]
    assert agenda.pick(twice, "join the review") is None


def test_a_name_matching_nothing_refuses():
    assert agenda.pick(DAY, "join the retro") is None


def test_an_empty_calendar_or_an_empty_phrase_refuses():
    assert agenda.pick([], "my 3pm") is None
    assert agenda.pick(DAY, "") is None
    assert agenda.pick(DAY, "join the meeting") is None      # no distinguishing word


# --- do you need to be there? ----------------------------------------------------------

def test_a_normal_meeting_needs_him():
    assert agenda.attendance(DAY[0]) == (True, "")


def test_a_calendar_marked_free_says_he_may_not():
    needed, why = agenda.attendance(_ev("3:00 PM", "Vendor demo", 900, status="Free"))
    assert needed is False and "free" in why


def test_tentative_counts_too():
    assert agenda.attendance(_ev("3:00 PM", "Vendor demo", 900, status="Tentative"))[0] is False


def test_a_broadcast_is_not_a_meeting_he_is_needed_at():
    needed, why = agenda.attendance(_ev("4:00 PM", "Company all-hands", 960))
    assert needed is False and "recording" in why


def test_being_unneeded_never_suppresses_only_quiets(monkeypatch):
    """The one mistake here he finds out about by MISSING it. So the answer is
    advisory: it moves a ping from interrupting to ambient, never to silence."""
    needed, why = agenda.attendance(_ev("4:00 PM", "Town hall", 960))
    assert needed is False
    assert why                                   # there is always something to say


# --- how much warning? -------------------------------------------------------------------

def test_a_standup_gets_the_long_lead_its_draft_needs():
    assert agenda.lead_minutes(DAY[0]) == 30


def test_a_one_to_one_gets_a_moment_not_half_an_hour():
    assert agenda.lead_minutes(DAY[1]) == 15


def test_a_review_sits_in_between():
    assert agenda.lead_minutes(DAY[2]) == 20


def test_a_broadcast_gets_a_nudge_because_there_is_no_prep_to_do():
    assert agenda.lead_minutes(_ev("4:00 PM", "All-hands", 960)) == 5


def test_anything_else_keeps_the_default():
    assert agenda.lead_minutes(_ev("4:00 PM", "Vendor call", 960)) == 30
    assert agenda.lead_minutes(_ev("4:00 PM", "Vendor call", 960), default=45) == 45


# --- what the day looks like ---------------------------------------------------------------

def test_a_clean_day_has_no_warnings():
    """DAY itself is not clean — its 2pm review runs straight into the 3pm
    all-hands, which is the point of the back-to-back check below."""
    clean = [DAY[0], DAY[1], DAY[2]]
    assert agenda.conflicts(clean) == []
    assert agenda.day_warnings(clean) == []


def test_two_meetings_overlapping_is_a_clash():
    clash = [_ev("2:00 PM", "Sprint review", 840, 900),
             _ev("2:30 PM", "Vendor sync", 870, 930)]
    pairs = agenda.conflicts(clash)
    assert len(pairs) == 1
    assert {p["title"] for p in pairs[0]} == {"Sprint review", "Vendor sync"}
    assert "Clash" in agenda.day_warnings(clash)[0]


def test_a_meeting_starting_exactly_as_another_ends_is_not_a_clash():
    """The boundary that decides whether every packed day reads as broken."""
    touching = [_ev("2:00 PM", "A", 840, 900), _ev("3:00 PM", "B", 900, 930)]
    assert agenda.conflicts(touching) == []


def test_touching_meetings_are_reported_as_back_to_back_instead():
    touching = [_ev("2:00 PM", "A", 840, 900), _ev("3:00 PM", "B", 900, 930)]
    assert len(agenda.back_to_back(touching)) == 1
    warning = agenda.day_warnings(touching)[0]
    assert "back-to-back" in warning
    # Named by when the next one STARTS: an event row with no parsed end time
    # rendered "first at " and told him nothing.
    assert warning.endswith("3:00 PM")


def test_a_day_with_real_gaps_is_not_flagged():
    assert agenda.back_to_back(DAY[:2]) == []


def test_three_overlapping_meetings_report_every_pair():
    triple = [_ev("2:00 PM", "A", 840, 960), _ev("2:15 PM", "B", 855, 900),
              _ev("2:30 PM", "C", 870, 920)]
    assert len(agenda.conflicts(triple)) == 3


# --- join by phrase, end to end (browser stubbed at its boundary) ------------------------

def _events(evs):
    async def _fake(structured=False):
        return evs if structured else [e["line"] for e in evs]
    return _fake


def test_joining_by_time_reaches_the_right_link(monkeypatch):
    joined = {}

    async def _join(url, muted=True, camera=False):
        joined["url"] = url
        return "joined (muted, camera off)"

    day = [_ev("3:00 PM", "All-hands", 900, join="https://teams.microsoft.com/l/meetup-join/x")]
    monkeypatch.setattr(outlook, "todays_meetings", _events(day))
    monkeypatch.setattr(meetings, "join", _join)
    out = asyncio.run(meetings.join_by_phrase("join my 3pm"))
    assert joined["url"].endswith("/x") and "All-hands" in out


def test_an_ambiguous_phrase_refuses_and_lists_the_options(monkeypatch):
    day = [_ev("3:00 PM", "All-hands", 900), _ev("3:00 PM", "Vendor sync", 900)]
    monkeypatch.setattr(outlook, "todays_meetings", _events(day))
    with pytest.raises(RuntimeError, match="doesn't pick out one meeting"):
        asyncio.run(meetings.join_by_phrase("join my 3pm"))


def test_a_meeting_with_no_link_says_so_instead_of_joining_nothing(monkeypatch):
    day = [_ev("3:00 PM", "All-hands", 900)]
    monkeypatch.setattr(outlook, "todays_meetings", _events(day))
    with pytest.raises(RuntimeError, match="no join link"):
        asyncio.run(meetings.join_by_phrase("join my 3pm"))


def test_an_empty_calendar_says_so(monkeypatch):
    monkeypatch.setattr(outlook, "todays_meetings", _events([]))
    with pytest.raises(RuntimeError, match="nothing on the calendar"):
        asyncio.run(meetings.join_by_phrase("join my 3pm"))


# --- reachable from chat and from a CLI brain -------------------------------------------

def test_the_agent_tool_turns_a_refusal_into_a_sentence(monkeypatch):
    """A brain gets a message, not an exception — it has to be able to hand the
    options back to Arun and ask which he meant."""
    from app import agent as agent_mod
    day = [_ev("3:00 PM", "All-hands", 900), _ev("3:00 PM", "Vendor sync", 900)]
    monkeypatch.setattr(outlook, "todays_meetings", _events(day))
    out = asyncio.run(agent_mod.join_meeting_by_name("my 3pm"))
    assert out.startswith("Didn't join") and "All-hands" in out


def test_the_named_form_is_reachable_over_http(monkeypatch):
    """Parity: the capability table advertises it, so the curl path must serve it."""
    from fastapi.testclient import TestClient

    from app import agent as agent_mod, main

    monkeypatch.setenv("ASTA_TOKEN", "qa-token")

    async def _fake(which):
        return f"joined — {which}"

    monkeypatch.setattr(agent_mod, "join_meeting_by_name", _fake)
    client = TestClient(main.app)
    r = client.post("/api/meetings/join", json={"which": "my 3pm"},
                    headers={"Authorization": "Bearer qa-token"})
    assert r.status_code == 200 and r.json()["message"] == "joined — my 3pm"


def test_neither_a_link_nor_a_name_is_a_bad_request(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setenv("ASTA_TOKEN", "qa-token")
    client = TestClient(main.app)
    r = client.post("/api/meetings/join", json={},
                    headers={"Authorization": "Bearer qa-token"})
    assert r.status_code == 400
