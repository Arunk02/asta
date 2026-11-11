""""Join the X meeting" when the calendar row carries no link (11 Nov).

The scrape reads Outlook's row labels; this Outlook build puts no Teams link in
them, and the fallback the code promised — open the event — did not exist, so
every "join my meeting" without a pasted link asked him for the link. Synthetic
meetings and links throughout.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from app import meetings, outlook, store

LINK = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_TEST%40thread.v2/0?context=x"
ROW = "Code Review, 12:30 PM to 1:00 PM, By Alex Kumar, Tentative"


def test_a_row_is_read_with_its_label_so_the_event_can_be_opened():
    ev, = outlook._events_from([ROW])
    assert ev["label"] == ROW and ev["join_url"] == ""


def test_a_long_row_that_carries_its_link_is_kept():
    long_row = f"Code Review, 12:30 PM to 1:00 PM, By Alex Kumar, Busy, {'agenda ' * 40}{LINK}"
    assert len(long_row) > 300
    ev, = outlook._events_from([long_row])
    assert ev["join_url"] == LINK


def test_a_link_wrapped_by_safe_links_is_found():
    from urllib.parse import quote
    wrapped = f"https://emea01.safelinks.protection.outlook.com/?url={quote(LINK, safe='')}&data=1"
    assert outlook.join_url_in(wrapped) == LINK


class FakePage:
    def __init__(self, text="", button_opens=""):
        self.text, self.button_opens, self.clicked, self.pressed = text, button_opens, [], []
        self.url = outlook.CALENDAR_URL
        self.context = self

    async def query_selector(self, sel):
        return True

    def locator(self, sel):
        page = self

        class Row:
            first = None

            async def click(self, timeout=0):
                page.clicked.append(sel)
        r = Row()
        r.first = r
        return r

    async def evaluate(self, js):
        return self.text

    def get_by_role(self, role, name=None):
        page = self

        class El:
            first = None

            async def count(self):
                return 1 if (role == "button" and page.button_opens) else 0

            async def get_attribute(self, name):
                return ""

            async def click(self, timeout=0):
                page.clicked.append("Join")
        e = El()
        e.first = e
        return e

    @contextlib.asynccontextmanager
    async def expect_page(self, timeout=0):
        page = self

        class New:
            url = page.button_opens
            closed = False

            async def close(self):
                New.closed = True
                page.closed_new = True

        class Info:
            value = None
        info = Info()
        yield info
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(New())
        info.value = fut

    @property
    def keyboard(self):
        page = self

        class K:
            async def press(self, key):
                page.pressed.append(key)
        return K()


@pytest.fixture
def calendar(monkeypatch):
    holder = {}

    @contextlib.asynccontextmanager
    async def site_page(url, **kw):
        yield holder["page"]
    monkeypatch.setattr(outlook.teams_bridge, "site_page", site_page)
    real_sleep = asyncio.sleep

    async def no_wait(*_a, **_k):
        await real_sleep(0)
    monkeypatch.setattr(outlook.asyncio, "sleep", no_wait)
    return holder


def _ev():
    ev, = outlook._events_from([ROW])
    return ev


def test_the_link_is_read_from_the_opened_event(calendar):
    calendar["page"] = FakePage(text=f"Microsoft Teams meeting\nJoin: {LINK}\nMeeting ID: 1")
    assert asyncio.run(outlook.join_url_for(_ev())) == LINK
    assert calendar["page"].clicked and calendar["page"].pressed == ["Escape"]


def test_failing_that_where_the_join_button_leads_closed_at_once(calendar):
    launcher = "https://teams.microsoft.com/dl/launcher/launcher.html?url=" + \
        "%2F_%23%2Fl%2Fmeetup-join%2F19%253ameeting_TEST%2540thread.v2%2F0"
    calendar["page"] = FakePage(text="Code Review\nNo body", button_opens=launcher)
    url = asyncio.run(outlook.join_url_for(_ev()))
    assert url.startswith("https://teams.microsoft.com/l/meetup-join/19")
    assert getattr(calendar["page"], "closed_new", False), "the page the button opened is closed"


def test_it_looks_once_a_day_per_meeting(calendar):
    calendar["page"] = FakePage(text=LINK)
    asyncio.run(outlook.join_url_for(_ev()))
    calendar["page"] = FakePage(text="nothing now")
    assert asyncio.run(outlook.join_url_for(_ev())) == LINK


def test_join_uses_the_events_own_link(monkeypatch):
    ev = _ev()

    async def today(structured=False):
        return [dict(ev)]

    async def look(e):
        return LINK

    async def join(url, speak=False, **kw):
        return f"joined {url[:40]}"
    monkeypatch.setattr(outlook, "todays_meetings", today)
    monkeypatch.setattr(outlook, "join_url_for", look)
    monkeypatch.setattr(meetings, "join", join)
    out = asyncio.run(meetings.join_by_phrase("code review", now_minutes=12 * 60 + 29))
    assert out.startswith("joined https://teams.microsoft.com") and "Code Review" in out


def test_no_link_anywhere_says_what_was_tried_and_asks_nothing(monkeypatch):
    ev = _ev()

    async def today(structured=False):
        return [dict(ev)]

    async def look(e):
        return ""
    monkeypatch.setattr(outlook, "todays_meetings", today)
    monkeypatch.setattr(outlook, "join_url_for", look)
    with pytest.raises(RuntimeError) as err:
        asyncio.run(meetings.join_by_phrase("code review", now_minutes=12 * 60 + 29))
    assert "opened it in Outlook" in str(err.value) and "send me the link" not in str(err.value)
    assert store.kv_get("teams_in_call") is None
