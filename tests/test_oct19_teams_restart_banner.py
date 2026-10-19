"""19 Oct: on relaunch Teams showed "Language changes detected (English). Please
restart Teams to see the change." in front of the app. Every Outlook read — they
open in the same pooled browser — failed "Teams app did not load within 75s"
for 12½ hours. The banner is answered once: its button, else a reload."""

import asyncio

from app import store, teams_bridge

BANNER = "Language changes detected (English). Please restart Teams to see the change. Apply"


class _Button:
    def __init__(self, page, present):
        self.page, self.present, self.first = page, present, self

    async def count(self):
        return 1 if self.present else 0

    async def click(self, timeout=0):
        self.page.clicked = True
        self.page.ready = True


class _Page:
    def __init__(self, button=True, banner=True):
        self.url = "https://teams.cloud.microsoft/"
        self.ready, self.clicked, self.reloaded = False, False, False
        self.button, self.banner = button, banner

    async def goto(self, *a, **k):
        pass

    async def query_selector(self, sel):
        return object() if self.ready else None

    async def evaluate(self, js, *a):
        return BANNER if self.banner and not self.ready else ""

    def get_by_role(self, role, name=None):
        return _Button(self, self.button)

    async def reload(self, **k):
        self.reloaded = True
        self.ready = True


class _Ctx:
    def __init__(self, page):
        self.pages = [page]


def _outcomes():
    with store._connect() as conn:
        return [r[0] for r in conn.execute("SELECT detail FROM outcomes WHERE outcome='restart banner'")]


def test_the_banner_s_button_is_pressed_and_teams_loads():
    page = _Page(button=True)
    assert asyncio.run(teams_bridge._open_teams(_Ctx(page), timeout=5)) is page
    assert page.clicked and "pressed its restart" in _outcomes()[0]


def test_without_a_button_the_page_is_reloaded():
    page = _Page(button=False)
    assert asyncio.run(teams_bridge._open_teams(_Ctx(page), timeout=5)) is page
    assert page.reloaded and not page.clicked


def test_no_banner_nothing_is_pressed():
    page = _Page(banner=False)
    page.ready = True
    assert asyncio.run(teams_bridge._open_teams(_Ctx(page), timeout=5)) is page
    assert not page.clicked and _outcomes() == []
