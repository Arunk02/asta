"""Reading a Teams thread with a WHEN in the question.

The reported failure: "check last night's message from Vinish about a bug" came
back with the wrong thing. It could not have come back with the right thing —
`read_chat` ran one querySelectorAll over whatever Teams had rendered and
returned "Sender: text" with no time attached. Nothing to filter on, no way to
reach anything older, and nothing kept.

So these tests cover the three halves of that: times are extracted, older
messages are actually fetched, and what is read survives to answer the next
question.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import agent, store, teams_bridge


# --- storage ------------------------------------------------------------------

def _msg(text, sender="Vinish Kumar", at=None, chat="Vinish Kumar"):
    return {"key": teams_bridge._msg_key(chat, {"sender": sender, "text": text,
                                                "iso": str(at)}),
            "chat": chat, "sender": sender, "text": text,
            "sent_at": at, "stamp": ""}


def test_a_thread_read_twice_is_not_stored_twice():
    """Every read re-sees the same recent messages; without dedupe the thread doubles."""
    rows = [_msg("build is red", at=1_786_500_000.0)]
    assert store.save_teams_messages(rows) == 1
    assert store.save_teams_messages(rows) == 0
    assert len(store.teams_messages("Vinish")) == 1


def test_messages_come_back_oldest_first():
    store.save_teams_messages([
        _msg("second", at=2000.0),
        _msg("first", at=1000.0),
    ])
    got = [r["text"] for r in store.teams_messages("Vinish")]
    assert got == ["first", "second"]


def test_a_window_selects_only_what_falls_inside_it():
    last_night = dt.datetime(2026, 8, 11, 21, 14).timestamp()
    this_morning = dt.datetime(2026, 8, 12, 9, 30).timestamp()
    store.save_teams_messages([
        _msg("the bug is in TmsServiceImpl", at=last_night),
        _msg("morning standup at 10", at=this_morning),
    ])
    window_start = dt.datetime(2026, 8, 11, 18, 0).timestamp()
    window_end = dt.datetime(2026, 8, 12, 6, 0).timestamp()

    got = store.teams_messages("Vinish", since=window_start, until=window_end)
    assert [r["text"] for r in got] == ["the bug is in TmsServiceImpl"]


def test_an_untimed_message_is_never_claimed_to_be_from_last_night():
    """Teams renders some rows with no machine-readable time.

    Including them in a window would silently reassign a message to an evening
    it may not belong to — a confident wrong answer, which is the whole thing
    this change exists to stop.
    """
    store.save_teams_messages([
        _msg("no timestamp on this one", at=None),
        _msg("timed", at=1_786_500_000.0),
    ])
    windowed = store.teams_messages("Vinish", since=0, until=9_999_999_999)
    assert [r["text"] for r in windowed] == ["timed"]
    # …but it is still there when no window is asked for.
    assert len(store.teams_messages("Vinish")) == 2


def test_untimed_messages_sort_last_rather_than_first():
    """NULL sorts first in SQLite; an untimed row must not pose as the oldest."""
    store.save_teams_messages([_msg("undated", at=None), _msg("dated", at=500.0)])
    assert [r["text"] for r in store.teams_messages("Vinish")] == ["dated", "undated"]


def test_he_asks_for_vinish_and_the_thread_was_stored_as_vinish_kumar():
    store.save_teams_messages([_msg("hi", chat="Vinish Kumar")])
    assert len(store.teams_messages("vinish")) == 1


# --- extraction ---------------------------------------------------------------

def test_an_iso_timestamp_becomes_an_epoch():
    got = teams_bridge._to_epoch("2026-08-11T21:14:00+00:00")
    assert got == dt.datetime(2026, 8, 11, 21, 14, tzinfo=dt.timezone.utc).timestamp()


def test_a_z_suffixed_timestamp_parses():
    assert teams_bridge._to_epoch("2026-08-11T21:14:00.000Z") is not None


def test_an_unparseable_time_yields_none_rather_than_a_guess():
    assert teams_bridge._to_epoch("9:14 PM") is None
    assert teams_bridge._to_epoch("") is None


def test_the_same_message_keys_the_same_way_every_read():
    a = {"sender": "Vinish", "text": "build is red", "iso": "2026-08-11T21:14:00Z"}
    assert teams_bridge._msg_key("chat", a) == teams_bridge._msg_key("chat", dict(a))


def test_two_different_messages_do_not_collide():
    a = {"sender": "Vinish", "text": "build is red", "iso": "2026-08-11T21:14:00Z"}
    b = {"sender": "Vinish", "text": "build is green", "iso": "2026-08-11T21:14:00Z"}
    assert teams_bridge._msg_key("chat", a) != teams_bridge._msg_key("chat", b)


def test_the_same_words_in_two_threads_are_two_messages():
    m = {"sender": "Vinish", "text": "ok", "iso": "2026-08-11T21:14:00Z"}
    assert teams_bridge._msg_key("Vinish Kumar", m) != teams_bridge._msg_key("Triage", m)


def test_a_formatted_line_leads_with_the_time():
    when = dt.datetime(2026, 8, 11, 21, 14).timestamp()
    line = teams_bridge.fmt_message(
        {"sent_at": when, "sender": "Vinish Kumar", "text": "build is red"})
    assert line.startswith("[Tue 11 Aug 21:14] ")
    assert "Vinish Kumar: build is red" in line


def test_a_line_with_no_time_falls_back_to_what_teams_rendered():
    line = teams_bridge.fmt_message(
        {"sent_at": None, "stamp": "Yesterday 9:14 PM", "sender": "V", "text": "x"})
    assert line.startswith("[Yesterday 9:14 PM] ")


def test_a_line_with_nothing_at_all_still_reads_cleanly():
    assert teams_bridge.fmt_message({"sender": "V", "text": "x"}) == "V: x"


# --- scrollback ---------------------------------------------------------------

class FakePage:
    """A thread that only reveals older messages when it is scrolled.

    This is the behaviour that made the original read unable to answer: Teams
    virtualises the list, so what is not on screen genuinely is not in the DOM.
    """

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages          # progressively longer views of the thread
        self.at = 0
        self.scrolls = 0

    async def evaluate(self, script, *args):
        if "scrollTop" in script:
            self.scrolls += 1
            if self.at < len(self.pages) - 1:
                self.at += 1
            return True
        return list(self.pages[self.at])


def _iso(day, hour, minute=0):
    return dt.datetime(2026, 8, day, hour, minute).astimezone().isoformat()


@pytest.fixture
def fake_teams(monkeypatch):
    """Wire read_history to a fake browser."""
    holder = {}

    def install(pages):
        page = FakePage(pages)
        holder["page"] = page

        class Ctx:
            pages = []
            async def close(self): pass

        class PW:
            async def stop(self): pass

        async def launch(headless=True):
            return PW(), Ctx()

        async def open_teams(ctx, timeout=75.0):
            return page

        async def find_chat(p, chat, allow_group=False):
            return chat

        async def title(p):
            return "Vinish Kumar"

        monkeypatch.setattr(teams_bridge, "_launch", launch)
        monkeypatch.setattr(teams_bridge, "_open_teams", open_teams)
        monkeypatch.setattr(teams_bridge, "_find_chat", find_chat)
        monkeypatch.setattr(teams_bridge, "_chat_title", title)
        monkeypatch.setattr(teams_bridge.asyncio, "sleep", _no_wait)
        return page

    return install


async def _no_wait(*a, **k):
    return None


@pytest.mark.asyncio
async def test_it_scrolls_back_until_it_reaches_last_night(fake_teams):
    """The actual reported bug: last night's message is not on screen."""
    on_screen = [{"sender": "Vinish Kumar", "text": "morning", "iso": _iso(12, 9), "stamp": ""}]
    after_one_scroll = [
        {"sender": "Vinish Kumar", "text": "the bug is in TmsServiceImpl",
         "iso": _iso(11, 21, 14), "stamp": ""},
    ] + on_screen
    page = fake_teams([on_screen, after_one_scroll])

    since = dt.datetime(2026, 8, 11, 18, 0).timestamp()
    rows = await teams_bridge.read_history("Vinish", since=since)

    assert page.scrolls >= 1, "never scrolled — cannot have reached last night"
    assert any("TmsServiceImpl" in r["text"] for r in rows)


@pytest.mark.asyncio
async def test_it_stops_scrolling_once_the_window_is_covered(fake_teams):
    """Scrolling further costs seconds and buys nothing."""
    covered = [{"sender": "V", "text": "old", "iso": _iso(10, 8), "stamp": ""}]
    page = fake_teams([covered, covered, covered])

    since = dt.datetime(2026, 8, 11, 18, 0).timestamp()
    await teams_bridge.read_history("Vinish", since=since)
    assert page.scrolls == 0, "scrolled past a window that was already covered"


@pytest.mark.asyncio
async def test_it_gives_up_at_the_top_of_the_thread(fake_teams):
    """A pane that stops growing must not be scrolled forever."""
    same = [{"sender": "V", "text": "only message", "iso": _iso(12, 9), "stamp": ""}]
    page = fake_teams([same])

    since = dt.datetime(2020, 1, 1).timestamp()   # unreachably old
    await teams_bridge.read_history("Vinish", since=since)
    assert page.scrolls <= 2, f"spun against an unmoving pane ({page.scrolls} scrolls)"


@pytest.mark.asyncio
async def test_a_plain_read_does_not_pay_for_scrollback(fake_teams):
    """"The last 15 messages" is already on screen."""
    on_screen = [{"sender": "V", "text": "hi", "iso": _iso(12, 9), "stamp": ""}]
    page = fake_teams([on_screen, on_screen])
    await teams_bridge.read_chat("Vinish", limit=15)
    assert page.scrolls == 0


@pytest.mark.asyncio
async def test_reading_a_thread_writes_it_to_history(fake_teams):
    """Which is what makes the second question about the same evening instant."""
    on_screen = [{"sender": "Vinish Kumar", "text": "build is red",
                  "iso": _iso(12, 9), "stamp": ""}]
    fake_teams([on_screen])
    await teams_bridge.read_chat("Vinish", limit=15)
    assert [r["text"] for r in store.teams_messages("Vinish")] == ["build is red"]


@pytest.mark.asyncio
async def test_read_chat_lines_now_carry_their_time(fake_teams):
    """The old return shape, with the thing that was missing added."""
    fake_teams([[{"sender": "Vinish Kumar", "text": "build is red",
                  "iso": _iso(11, 21, 14), "stamp": ""}]])
    lines = await teams_bridge.read_chat("Vinish", limit=5)
    assert lines == ["[Tue 11 Aug 21:14] Vinish Kumar: build is red"]


@pytest.mark.asyncio
async def test_history_survives_a_store_that_is_down(fake_teams, monkeypatch):
    """Keeping history is a bonus on top of the read, never a reason to fail it."""
    def explode(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(store, "save_teams_messages", explode)
    fake_teams([[{"sender": "V", "text": "still returned", "iso": _iso(12, 9), "stamp": ""}]])
    lines = await teams_bridge.read_chat("Vinish", limit=5)
    assert any("still returned" in line for line in lines)


# --- the agent tool -----------------------------------------------------------

@pytest.fixture
def teams_on(monkeypatch):
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)


@pytest.mark.asyncio
async def test_the_tool_answers_from_stored_history_without_a_browser(teams_on, monkeypatch):
    """Asking twice about the same evening must not cost another 20-second scrape.

    The setup is deliberately a thread that was genuinely read ACROSS the window:
    a message from before it opened (so the stored history reaches back past the
    start) and a last-read stamp from after it closed (so nothing arrived
    unseen). An earlier version of this test stored a single message inside the
    window and asserted the same thing — which is not a cache hit, it is the bug
    in `teams_history` that reported one message as a whole evening. The test
    passed for as long as the bug existed.
    """
    async def must_not_run(*a, **k):
        raise AssertionError("opened a browser when history already had the answer")

    monkeypatch.setattr(teams_bridge, "read_history", must_not_run)

    now = dt.datetime.now()
    last_night = (now.replace(hour=21, minute=14, second=0, microsecond=0)
                  - dt.timedelta(days=1)).timestamp()
    store.save_teams_messages([
        _msg("earlier in the day", at=last_night - 6 * 3600),
        _msg("the bug is in TmsServiceImpl", at=last_night),
    ])
    # Read after the window closed — this morning.
    with store._connect() as c:
        c.execute("UPDATE teams_messages SET seen_at=? WHERE chat=?",
                  (now.timestamp(), "Vinish Kumar"))

    out = await agent.teams_history("Vinish", when="last night")
    assert "TmsServiceImpl" in out
    assert "stored history" in out


@pytest.mark.asyncio
async def test_the_tool_says_what_window_it_looked_at(teams_on, monkeypatch):
    async def nothing(*a, **k):
        return []

    monkeypatch.setattr(teams_bridge, "read_history", nothing)
    out = await agent.teams_history("Vinish", when="last night")
    assert "last night" in out
    assert "→" in out, "did not state the window it searched"


@pytest.mark.asyncio
async def test_an_empty_window_says_so_instead_of_returning_todays_messages(
        teams_on, monkeypatch):
    """The original failure mode was answering with the wrong messages, silently."""
    async def nothing(*a, **k):
        return []

    monkeypatch.setattr(teams_bridge, "read_history", nothing)
    now = dt.datetime.now().timestamp()
    store.save_teams_messages([_msg("todays chatter", at=now)])

    out = await agent.teams_history("Vinish", when="last night")
    assert "todays chatter" not in out
    assert "Nothing found" in out


@pytest.mark.asyncio
async def test_an_expired_session_is_reported_as_itself(teams_on, monkeypatch):
    async def expired(*a, **k):
        raise RuntimeError("SESSION_EXPIRED")

    monkeypatch.setattr(teams_bridge, "read_history", expired)
    out = await agent.teams_history("Vinish", when="last night")
    assert "session expired" in out.lower()


@pytest.mark.asyncio
async def test_the_tool_is_honest_when_the_bridge_is_off(monkeypatch):
    monkeypatch.setattr(teams_bridge, "enabled", lambda: False)
    out = await agent.teams_history("Vinish")
    assert "off" in out.lower()
