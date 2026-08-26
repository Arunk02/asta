"""The extraction JS, run against the DOM Teams actually ships.

Every other test in this area feeds Python dicts to Python functions, which
proves the plumbing and nothing about the one part that talks to Teams. That gap
is not theoretical: the first version of `_MESSAGE_JS` looked for `<time
datetime=...>` and `[data-tid="message-timestamp"]`, both of which are perfectly
reasonable and neither of which exists in this Teams build. Every message came
back with no time, and the code around it worked flawlessly on the nothing it
was given.

What Teams really does — read off the live DOM, not guessed — is put the send
time in the id of the message content div:

    <div id="content-1786525691522">Bro, assigned this defect to you…</div>

Thirteen digits of epoch milliseconds. So the fixture below is a reduction of a
real captured message row, and these tests run the real script in a real browser
against it. If Teams changes that shape the tests fail here, loudly, instead of
in six months when he asks what someone said last night.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio

from app import teams_bridge

pytestmark = pytest.mark.asyncio


def _row(content_id: str, author: str, text: str, extra: str = "") -> str:
    """One message row, shaped like the real thing."""
    return f"""
    <div data-tid="chat-pane-item" data-person-mri="8:orgid:abc" {extra}>
      <span data-tid="message-author-name">{author}</span>
      <div data-tid="messageBodyContent" id="{content_id}">{text}</div>
    </div>
    """


PAGE = "<html><body>" + _row(
    "content-1786525691522", "Vinish Kumar",
    "Bro, assigned this defect to you") + "</body></html>"


@pytest_asyncio.fixture
async def browser():
    """A real Chromium. Skips rather than fails where browsers aren't installed."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:                                  # pragma: no cover
        pytest.skip("playwright not installed")
    pw = await async_playwright().start()
    try:
        b = await pw.chromium.launch(headless=True)
    except Exception as exc:                             # pragma: no cover
        await pw.stop()
        pytest.skip(f"no chromium available: {exc}")
    page = await b.new_page()
    yield page
    await b.close()
    await pw.stop()


async def _extract(page, html):
    await page.set_content(html)
    return await page.evaluate(teams_bridge._MESSAGE_JS, 0)


async def test_the_send_time_is_read_out_of_the_content_id(browser):
    """The whole point. 1786525691522ms is a real captured message time."""
    got = await _extract(browser, PAGE)
    assert len(got) == 1
    assert got[0]["sender"] == "Vinish Kumar"
    assert got[0]["iso"], "no timestamp extracted — the reported bug, exactly"

    when = dt.datetime.fromtimestamp(teams_bridge._to_epoch(got[0]["iso"]))
    assert (when.year, when.month, when.day) == (2026, 8, 12)


async def test_a_time_element_still_wins_where_teams_ships_one(browser):
    """Other Teams builds do have <time>; supporting both is the point of the chain."""
    html = """<html><body>
      <div data-tid="chat-pane-item">
        <span data-tid="message-author-name">Suraj</span>
        <time datetime="2026-08-11T21:14:00.000Z">9:14 PM</time>
        <div data-tid="messageBodyContent" id="content-1786525691522">hi</div>
      </div></body></html>"""
    got = await _extract(browser, html)
    assert got[0]["iso"].startswith("2026-08-11T21:14")


async def test_a_message_with_no_usable_time_reports_none_rather_than_guessing(browser):
    """A wrong time silently reassigns a message to the wrong evening."""
    html = """<html><body>
      <div data-tid="chat-pane-item">
        <span data-tid="message-author-name">Suraj</span>
        <div data-tid="messageBodyContent">no id at all</div>
      </div></body></html>"""
    got = await _extract(browser, html)
    assert got[0]["iso"] == ""
    assert teams_bridge._to_epoch(got[0]["iso"]) is None


async def test_a_thirteen_digit_id_that_is_not_a_time_is_rejected(browser):
    """Plenty of ids are thirteen digits long without being milliseconds."""
    html = """<html><body>
      <div data-tid="chat-pane-item">
        <span data-tid="message-author-name">Suraj</span>
        <div data-tid="messageBodyContent" id="content-1000000000000">x</div>
      </div></body></html>"""
    got = await _extract(browser, html)
    # 1e12 ms is 2001 — long before Teams existed, so it is not a send time.
    assert got[0]["iso"] == ""


async def test_messages_come_back_in_thread_order(browser):
    html = "<html><body>" + _row("content-1786525691522", "V", "first") \
         + _row("content-1786525891522", "A", "second") + "</body></html>"
    got = await _extract(browser, html)
    assert [m["text"] for m in got] == ["first", "second"]


async def test_one_row_yields_one_message_not_two(browser):
    """The selector matches two nodes per message; without dedupe every line doubles."""
    got = await _extract(browser, PAGE)
    assert len(got) == 1


async def test_the_limit_takes_the_most_recent(browser):
    html = "<html><body>" + "".join(
        _row(f"content-17865256915{i:02d}", "V", f"m{i}") for i in range(5)) + "</body></html>"
    await browser.set_content(html)
    got = await browser.evaluate(teams_bridge._MESSAGE_JS, 2)
    assert [m["text"] for m in got] == ["m3", "m4"]


async def test_an_empty_message_is_skipped(browser):
    """Reactions and system rows render as empty bodies."""
    html = "<html><body>" + _row("content-1786525691522", "V", "") \
         + _row("content-1786525891522", "A", "real") + "</body></html>"
    got = await _extract(browser, html)
    assert [m["text"] for m in got] == ["real"]


async def test_a_message_from_arun_has_no_author_element(browser):
    """Teams omits the author on your own messages; 'me' is the honest label."""
    html = """<html><body>
      <div data-tid="chat-pane-item">
        <div data-tid="messageBodyContent" id="content-1786525691522">mine</div>
      </div></body></html>"""
    got = await _extract(browser, html)
    assert got[0]["sender"] == "me"


async def test_the_scroll_script_finds_a_scrollable_pane(browser):
    """Scrollback is what reaches last night; it needs the right container."""
    html = """<html><body>
      <div data-tid="message-pane-list-viewport"
           style="height:100px;overflow:auto">
        <div style="height:5000px">tall</div>
      </div></body></html>"""
    await browser.set_content(html)
    assert await browser.evaluate(teams_bridge._SCROLL_JS) is True


async def test_the_scroll_script_reports_when_there_is_nothing_to_scroll(browser):
    """So read_history stops instead of spinning against an unmoving pane."""
    await browser.set_content("<html><body><div>short</div></body></html>")
    assert await browser.evaluate(teams_bridge._SCROLL_JS) is False
