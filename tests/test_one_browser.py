"""One Chrome on the Teams profile. Nothing launches a second one beside it.

29 Sep: 1,045 "orphaned browser reaped" in seven days, 88 of them that day,
every ~5.3 minutes. The Outlook watcher (OUTLOOK_POLL=300) took the shared lock
and then LAUNCHED ITS OWN Chrome on the same profile — written before the
Teams browser became a long-lived pool. The launch collided with the pool,
recovery reaped the "orphan" — which was the pool's own live Teams browser —
Outlook read mail in a fresh Chrome and closed it, and the next Teams read
cold-booted Teams again. Every five minutes, all day.

`site_page` is the way to open another corporate site in the same profile: a
tab in the pooled browser, under the same lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import re

import pytest


def test_only_the_bridge_ever_launches_a_browser_on_the_profile():
    """A headless read goes through `site_page`. The only launches allowed outside
    the bridge are EXCLUSIVE windows — a call, the voice self-test, a sign-in —
    and each must close the pool first, so it replaces the pooled browser rather
    than colliding with it."""
    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if path.name == "teams_bridge.py":
            continue
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"teams_bridge\._launch\(", line):
                continue
            before = "\n".join(lines[max(0, i - 3):i])
            if "headless=False" not in line or "close_pool()" not in before:
                offenders.append(f"{path}:{i + 1}")
    assert not offenders, (
        "these start a second Chrome beside the pooled one, which gets it killed — "
        "use teams_bridge.site_page, or close_pool() first for a window that must "
        "own the profile:\n  " + "\n  ".join(offenders))


class _Tab:
    url = "https://outlook.office.com/mail/"

    async def query_selector(self, sel):
        return object()

    async def evaluate(self, js, *a):
        if "role=\"option\"" in js:                     # the mail list
            return [{"aria": "Unread Navya R PR 1251 10:05", "text": "Navya R | PR 1251"}]
        return ["Standup, 10:00 to 10:15"]               # calendar labels


@pytest.fixture
def pooled(monkeypatch):
    from app import teams_bridge
    opened = []

    @contextlib.asynccontextmanager
    async def site_page(url, **k):
        opened.append(url)
        yield _Tab()

    async def no_launch(*a, **k):
        raise AssertionError("a second browser was launched on the profile")
    monkeypatch.setattr(teams_bridge, "site_page", site_page)
    monkeypatch.setattr(teams_bridge, "_launch", no_launch)
    return opened


def test_outlook_mail_is_read_in_a_tab_of_the_pooled_browser(pooled, monkeypatch):
    from app import outlook
    real = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: real(0))
    rows = asyncio.run(outlook.read_mail())
    assert pooled == [outlook.MAIL_URL] and rows and rows[0]["sender"] == "Navya R"


def test_the_calendar_is_read_the_same_way(pooled, monkeypatch):
    from app import outlook
    real = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: real(0))
    asyncio.run(outlook._todays_events())
    assert pooled == [outlook.CALENDAR_URL]
