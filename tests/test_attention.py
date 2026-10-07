"""One ledger, one decision — and a heartbeat so silence can be trusted.

Three guarantees, in the order they matter:

  1. Off by default it changes NOTHING. Every watcher behaves exactly as it did
     before the module existed, because `consider` waves everything through.
  2. On, the same thing wanting Arun is announced ONCE, whichever channel carried
     it — the cross-source collision that `goes_to_hold` had to fix by hand.
  3. The freshness heartbeat is live regardless of the flag, because a watcher
     that stopped reading looks exactly like a quiet week.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import attention, memory, outlook, store, teams_bridge, triage


@pytest.fixture(autouse=True)
def _quiet_local_model(monkeypatch):
    """triage.refine must never reach LM Studio in a test — and must be instant."""
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "1")


# --- the no-op contract ---------------------------------------------------------

def test_disabled_pushes_everything_and_records_nothing(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "0")
    assert attention.consider("outlook", "k1", who="Sam", what="ping") is True
    assert attention.consider("outlook", "k1", who="Sam", what="ping") is True
    assert store.attention_get("k1") is None       # nothing written at all


def test_an_empty_key_is_waved_through_rather_than_recorded(on):
    """A source that cannot identify its item must not create an unkeyed row that
    would then dedup against every OTHER unkeyed item."""
    assert attention.consider("outlook", "", what="no identity") is True
    assert attention.open_items() == []


# --- announce once, whatever carried it ------------------------------------------

def test_a_new_thing_is_pushed_and_recorded(on):
    assert attention.consider("outlook", "k1", who="Sam", what="Sam: approve?") is True
    row = store.attention_get("k1")
    assert row["state"] == "notified" and row["who"] == "Sam"


def test_the_same_thing_is_not_pushed_twice(on):
    attention.consider("outlook", "k1", what="approve?")
    assert attention.consider("outlook", "k1", what="approve?") is False


def test_two_sources_carrying_one_incident_announce_it_once(on):
    """The collision `goes_to_hold` was written to patch, solved generally."""
    assert attention.consider("outlook", "INC12345", what="pods down") is True
    assert attention.consider("teams", "INC12345", what="pods down") is False
    assert attention.consider("ci", "INC12345", what="pods down") is False
    assert store.attention_get("INC12345")["sources"] == "outlook,teams,ci"


def test_re_sighting_counts_the_chase_without_resetting_the_lifecycle(on):
    """seen_count climbing while state stays `notified` IS the chase signal —
    overwriting the row every poll is what would erase it."""
    attention.consider("outlook", "k1", what="any update?")
    for _ in range(3):
        attention.consider("outlook", "k1", what="any update?")
    row = store.attention_get("k1")
    assert row["seen_count"] == 4
    assert row["state"] == "notified"
    assert row["source"] == "outlook"          # first source kept


def test_a_thing_that_got_worse_is_re_ranked_up(on):
    """A warning that became an outage must not stay ranked as a warning."""
    attention.consider("outlook", "k1", what="latency high", priority=attention.P_FYI)
    attention.consider("outlook", "k1", what="pods down", priority=attention.P_NOW)
    row = store.attention_get("k1")
    assert row["priority"] == attention.P_NOW
    assert row["what"] == "pods down"           # the better description wins too


def test_a_thing_never_gets_re_ranked_down(on):
    attention.consider("outlook", "k1", what="pods down", priority=attention.P_NOW)
    attention.consider("teams", "k1", what="chatter", priority=attention.P_FYI)
    assert store.attention_get("k1")["priority"] == attention.P_NOW


# --- settled means settled --------------------------------------------------------

def test_something_he_acted_on_is_never_raised_again(on):
    attention.consider("outlook", "k1", what="approve?")
    attention.mark_acted("k1")
    assert attention.consider("teams", "k1", what="approve?") is False
    assert store.attention_get("k1")["state"] == "acted"


def test_something_dropped_is_never_raised_again(on):
    """An alert that recovered stopped mattering without him lifting a finger."""
    attention.consider("outlook", "k1", what="disk 90%")
    attention.mark_dropped("k1")
    assert attention.consider("outlook", "k1", what="disk 90%") is False


def test_a_muted_priority_is_recorded_but_never_pushed(on):
    """Suppression still leaves an audit trail — 'why didn't you tell me' has an
    answer, which a silent drop cannot give him."""
    assert attention.consider("outlook", "k1", what="newsletter",
                              priority=attention.P_MUTE) is False
    assert store.attention_get("k1") is not None


# --- what's on my plate -----------------------------------------------------------

def test_open_items_rank_urgent_first_then_oldest(on):
    attention.consider("outlook", "old-fyi", what="c", priority=attention.P_FYI, now=100)
    attention.consider("outlook", "new-now", what="a", priority=attention.P_NOW, now=300)
    attention.consider("outlook", "old-today", what="b", priority=attention.P_TODAY, now=100)
    attention.consider("outlook", "new-today", what="d", priority=attention.P_TODAY, now=400)
    assert [i["key"] for i in attention.open_items()] == [
        "new-now", "old-today", "new-today", "old-fyi"]


def test_open_items_leaves_out_what_is_settled(on):
    attention.consider("outlook", "done", what="a")
    attention.consider("outlook", "live", what="b")
    attention.mark_acted("done")
    assert [i["key"] for i in attention.open_items()] == ["live"]


def test_open_items_can_be_limited_to_the_things_that_want_something(on):
    attention.consider("outlook", "ask", what="a", priority=attention.P_TODAY)
    attention.consider("outlook", "fyi", what="b", priority=attention.P_FYI)
    keys = [i["key"] for i in attention.open_items(max_priority=attention.P_TODAY)]
    assert keys == ["ask"]


def test_purge_clears_settled_history_but_never_live_work(on):
    attention.consider("outlook", "old", what="a", now=0)
    attention.consider("outlook", "live", what="b", now=0)
    attention.mark_acted("old")
    store.attention_set("old", last_seen=0)
    store.attention_set("live", last_seen=0)
    assert attention.purge(days=14, now=100 * 86400) == 1
    assert [i["key"] for i in attention.open_items()] == ["live"]


# --- the heartbeat: silence must be explainable ------------------------------------

def test_a_source_that_never_ran_is_off_not_broken(monkeypatch):
    """Alarming about a Teams bridge he never enabled is crying wolf on day one."""
    monkeypatch.setenv("ASTA_ATTENTION", "0")
    assert attention.stale_sources(now=10**9) == {}


def test_a_watcher_that_never_once_succeeded_is_reported(monkeypatch):
    """The hole this closes, found live. 'Never reported = switched off' is right
    for a bridge he never enabled and WRONG for a scrape that broke on its first
    poll — which then never reports, and so is never called broken. That is the
    state the Teams activity watcher was actually in: enabled, session healthy,
    failing silently every five minutes while Outlook beside it ran fine."""
    monkeypatch.setenv("ASTA_ATTENTION", "0")
    attention.note_watching("teams", now=1000)
    assert attention.stale_sources(("teams",), now=1000 + 30 * 60) == {}   # grace
    assert attention.stale_sources(("teams",), now=1000 + 91 * 60) == {"teams": 91}
    assert attention.never_succeeded("teams") is True


def test_one_success_turns_a_never_worked_watcher_into_a_healthy_one():
    attention.note_watching("teams", now=1000)
    attention.note_scrape("teams", now=1000 + 60)
    assert attention.stale_sources(("teams",), now=1000 + 91 * 60) == {"teams": 90}
    assert attention.never_succeeded("teams") is False


def test_the_start_marker_is_not_reset_by_a_later_restart_of_the_loop():
    """Otherwise a loop that restarts often would keep resetting its own clock and
    never age past the window — the alarm could never fire."""
    attention.note_watching("teams", now=1000)
    attention.note_watching("teams", now=5000)
    assert attention.watching_since("teams") == 1000


def test_the_reason_a_scrape_failed_is_kept(monkeypatch):
    """Knowing it is broken says to look. Knowing it raised a selector timeout on
    the activity list says where."""
    attention.note_scrape_error("teams", TimeoutError("waiting for activity-list-container"))
    assert "TimeoutError" in attention.last_error("teams")
    assert "activity-list-container" in attention.last_error("teams")


def test_health_says_a_watcher_never_worked_and_why(monkeypatch):
    from app import health
    attention.note_watching("teams", now=1000)
    attention.note_scrape_error("teams", TimeoutError("activity-list-container"))
    monkeypatch.setattr(attention, "stale_sources", lambda *a, **k: {"teams": 120})
    problems = asyncio.run(health.checks())
    assert "never once read successfully" in problems["teams_watcher"]
    assert "activity-list-container" in problems["teams_watcher"]


def test_a_source_that_just_read_is_healthy():
    attention.note_scrape("outlook", now=1000)
    assert attention.stale_sources(("outlook",), now=1000 + 60) == {}


def test_a_source_that_worked_and_went_quiet_is_reported(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "0")   # heartbeat is NOT flagged
    attention.note_scrape("outlook", now=1000)
    stale = attention.stale_sources(("outlook",), now=1000 + 91 * 60)
    assert stale == {"outlook": 91}


def test_the_staleness_window_is_configurable_and_zero_disables_it(monkeypatch):
    attention.note_scrape("outlook", now=1000)
    monkeypatch.setenv("ASTA_STALE_AFTER_MINUTES", "10")
    assert attention.stale_sources(("outlook",), now=1000 + 11 * 60) == {"outlook": 11}
    monkeypatch.setenv("ASTA_STALE_AFTER_MINUTES", "0")
    assert attention.stale_sources(("outlook",), now=1000 + 999 * 60) == {}


def test_health_names_a_stale_watcher_as_a_problem(monkeypatch):
    from app import health
    attention.note_scrape("teams", now=1000)
    monkeypatch.setattr(attention, "stale_sources", lambda *a, **k: {"teams": 120})
    problems = asyncio.run(health.checks())
    assert "teams_watcher" in problems
    assert "120 min" in problems["teams_watcher"]


# --- wired into the real watchers --------------------------------------------------

class _Notify:
    """Stands in for the notify module: records what would have reached his phone."""

    def __init__(self):
        self.sent = []

    async def notify(self, text, level="info", urgency="direct", priority=None,
                     **kw):        # **kw: notify() also takes source/key/considered
        self.sent.append((text, urgency))
        return {"bell": True}


def _mail(sender, subject, preview=""):
    return {"unread": True, "important": False, "sender": sender,
            "subject": subject, "when": "9:00 AM", "preview": preview}


def test_outlook_announces_one_mail_once_across_polls(on):
    n = _Notify()
    m = _mail("Sam", "please review the deploy plan")
    asyncio.run(outlook._push_mail(n, [m]))
    asyncio.run(outlook._push_mail(n, [m]))          # same mail, next poll
    assert len(n.sent) == 1


def test_outlook_with_the_ledger_off_behaves_exactly_as_before(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "0")
    n = _Notify()
    m = _mail("Sam", "please review the deploy plan")
    asyncio.run(outlook._push_mail(n, [m]))
    asyncio.run(outlook._push_mail(n, [m]))
    assert len(n.sent) == 2                          # the old behaviour, untouched


def test_a_teams_mention_about_an_already_announced_incident_stays_quiet(on):
    """The cross-source win, end to end through BOTH real push paths.

    This is the test that caught the hole: mail keys on sender+subject and the
    Teams feed keys on its rendered row, so the same incident produced two keys
    and the unique column deduped nothing. The shared id join is what makes it
    real, and driving both live push functions is what proves it.
    """
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("ServiceNow", "INC4471 booking service down")]))
    assert len(n.sent) == 1

    asyncio.run(teams_bridge._push_activity(
        n, ["Priya — mentioned you in Platform: INC4471 is still down, can you look?"]))
    assert len(n.sent) == 1                            # still one — same incident
    assert store.attention_get("INC4471")["sources"] == "outlook,teams"


def test_things_that_merely_resemble_each_other_are_not_collapsed(on):
    """The other half of the contract: no id, no join. Two different people
    asking about 'the deploy' are two asks, and merging them would lose one."""
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Sam", "can you review the deploy plan?")]))
    asyncio.run(outlook._push_mail(n, [_mail("Priya", "can you review the deploy runbook?")]))
    assert len(n.sent) == 2


def test_a_jira_key_joins_a_mail_and_a_mention_too(on):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Jira", "PROJ-812 needs your sign-off")]))
    asyncio.run(teams_bridge._push_activity(n, ["Sam — mentioned you: PROJ-812 blocked on you"]))
    assert len(n.sent) == 1
    assert store.attention_get("PROJ-812") is not None


def test_key_for_prefers_a_real_id_over_the_wording_around_it():
    assert attention.key_for("ServiceNow", "INC4471 booking down") == "INC4471"
    assert attention.key_for("Sam — mentioned you: inc4471 is down") != "INC4471"  # case-real
    assert attention.key_for("Sam", "any update?") == triage.stable_key("Sam any update?")


def test_teams_announces_one_mention_once_across_polls(on):
    n = _Notify()
    item = "Priya — mentioned you in Platform: can you approve the release?"
    asyncio.run(teams_bridge._push_activity(n, [item]))
    asyncio.run(teams_bridge._push_activity(n, [item]))
    assert len(n.sent) == 1


def test_a_real_ask_still_reaches_him_directly(on):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Sam", "can you approve this by EOD?")]))
    assert n.sent and n.sent[0][1] == "direct"


def test_pure_fyi_still_rides_the_quiet_path(on):
    n = _Notify()
    asyncio.run(outlook._push_mail(n, [_mail("Sam", "FYI — notes from the sync")]))
    assert n.sent and n.sent[0][1] == "ambient"


# --- the Teams activity click, which was silently dead in production ------------

class _Page:
    """A page that detaches its Activity button the way Teams actually does."""

    def __init__(self, fail_times: int = 0, always_fail: bool = False, shortcut_works: bool = False,
                 state: dict | None = None, read_result: dict | None = None,
                 restore_fails: bool = False):
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.shortcut_works = shortcut_works
        self.state = {"app": True, **(state or {})}
        self.read_result = (read_result if read_result is not None else
                            {"valid": True, "rows": [{"text": "Sam mentioned you", "unread": True}]})
        self.restore_fails = restore_fails
        self.clicks = 0
        self.keys: list[str] = []
        self.waited = []
        self.opened = False
        self.chat = True
        self.navigated = []
        self.probes = 0
        self.url = "https://teams.cloud.microsoft/"
        page = self

        class _Response:
            ok = page.state.get("network", False)
            status = page.state.get("status", 200 if page.state.get("network") else 503)
            url = page.state.get("network_url", page.url)

            async def dispose(self):
                pass

        class _Request:
            async def get(self, url, **kwargs):
                page.probes += 1
                return _Response()

        class _Context:
            request = _Request()

        self.context = _Context()

        class _Loc:
            def __init__(self, sel):
                self.sel = sel
                self.first = self

            async def click(self, timeout=None):
                if "Activity" not in self.sel:
                    if page.restore_fails:
                        raise RuntimeError("Chat button did not respond")
                    page.chat = True
                    return
                page.clicks += 1
                if page.always_fail or page.clicks <= page.fail_times:
                    raise RuntimeError("Locator.click: Element is not attached to the DOM")
                page.opened = True
                page.chat = False

        class _Keys:
            async def press(self, key):
                page.keys.append(key)
                if key == "Control+Shift+1" and page.shortcut_works:
                    page.opened = True
                    page.chat = False

        self._Loc, self.keyboard = _Loc, _Keys()

    def locator(self, sel):
        return self._Loc(sel)

    def get_by_role(self, role, name=""):
        page = self

        class _Retry:
            async def click(self, timeout=None):
                page.keys.append(f"retry:{name}")
                page.state.pop("oops", None)
        return _Retry()

    def is_closed(self):
        return False

    async def evaluate(self, script, *a):
        if script == teams_bridge._ACTIVITY_ROWS_JS:
            return self.read_result
        if "document.querySelectorAll('[role=\"treeitem\"]')" in script:
            return 1 if self.chat else 0
        return dict(self.state)

    async def wait_for_selector(self, selector, timeout=None):
        self.waited.append(selector)
        if "activity-list-container" in selector and not self.opened:
            raise RuntimeError("Timeout waiting for the activity list")
        if '[data-tid="chat-list"]' in selector and not self.chat:
            raise RuntimeError("Timeout waiting for the chat list")
        if "app-bar" in selector and not self.state.get("app"):
            raise RuntimeError("Teams app did not reload")
        return object()

    async def goto(self, url, **kwargs):
        self.navigated.append(url)
        if self.restore_fails:
            raise RuntimeError("Teams did not reload")
        self.chat = bool(self.state.get("app"))


def test_opening_activity_survives_teams_re_rendering_under_it(monkeypatch):
    """The live failure: the rail re-renders between resolve and click, so a held
    handle is detached. Every poll raised, the watcher swallowed it, and a dead
    mention watcher looked exactly like a quiet afternoon."""
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    page = _Page(fail_times=2)
    asyncio.run(teams_bridge._open_activity(page))
    assert page.clicks == 3
    assert any("activity-list-container" in w for w in page.waited)


def test_opening_activity_succeeds_first_time_when_the_page_is_settled(monkeypatch):
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    page = _Page()
    asyncio.run(teams_bridge._open_activity(page))
    assert page.clicks == 1


def test_a_genuinely_broken_activity_tab_raises_rather_than_returning_empty(monkeypatch):
    """It must RAISE, so the watcher records the error and the heartbeat goes
    stale. Returning [] would read as 'no mentions' — the silent failure again."""
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    page = _Page(always_fail=True)
    with pytest.raises(RuntimeError, match="could not open the Teams Activity feed"):
        asyncio.run(teams_bridge._open_activity(page))
    assert page.clicks == teams_bridge._ACTIVITY_ATTEMPTS


async def _instant(seconds):
    return None


# --- 3 Oct: the Activity tab when Teams is not in its usual state -----------------

def test_a_covered_activity_button_is_reached_with_teams_own_shortcut(monkeypatch):
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    page = _Page(always_fail=True, shortcut_works=True, state={"dialogs": ["Teams notice"]})
    asyncio.run(teams_bridge._open_activity(page))
    assert "Control+Shift+1" in page.keys and "Escape" in page.keys


def test_oops_app_failed_to_load_is_retried_not_clicked_through(monkeypatch):
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    page = _Page(state={"oops": True})
    asyncio.run(teams_bridge._open_activity(page))
    assert "retry:Retry" in page.keys and page.opened


def test_a_sign_in_page_is_reported_and_the_session_flag_left_to_the_session_check(monkeypatch):
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    store.kv_set("teams_session_ok", "1")
    page = _Page(state={"signin": True, "url": "https://login.microsoftonline.com/x"})
    with pytest.raises(RuntimeError, match="SESSION_EXPIRED"):
        asyncio.run(teams_bridge._open_activity(page))
    assert page.clicks == 0
    assert store.kv_get("teams_session_ok") == "1", "a guess never stops every Teams read"
    assert any(r["outcome"] == "activity_unavailable" for r in store.recent_outcomes(5))


def test_a_failure_says_what_the_page_showed(monkeypatch):
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    page = _Page(always_fail=True, state={"title": "Chat | Microsoft Teams",
                                          "covered_by": "DIV What's new in Teams"})
    with pytest.raises(teams_bridge.ActivityUnavailable) as err:
        asyncio.run(teams_bridge._open_activity(page))
    assert "covered by DIV What's new in Teams" in str(err.value)
    with store._connect() as conn:
        got = conn.execute("SELECT detail FROM outcomes WHERE outcome='activity_unavailable'").fetchall()
    assert got and "What's new" in got[-1][0]


def test_an_activity_miss_sends_the_page_home_and_keeps_the_browser(monkeypatch):
    """Every miss used to relaunch Chrome (3 Oct, ~15 times)."""
    discarded = []
    page = _Page(always_fail=True)
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)

    async def pooled():
        return page

    async def discard(why=""):
        discarded.append(why)

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    monkeypatch.setattr(teams_bridge, "_discard_pool", discard)

    async def go():
        async with teams_bridge.teams_page():
            await teams_bridge._open_activity(page)
    with pytest.raises(teams_bridge.ActivityUnavailable):
        asyncio.run(go())
    assert page.navigated == [teams_bridge.TEAMS_URL] and not discarded


def test_after_reading_activity_the_page_goes_back_to_chat():
    clicked = []

    class P:
        def locator(self, sel):
            class L:
                first = None

                async def click(self, timeout=None):
                    clicked.append(sel)
            loc = L()
            loc.first = loc
            return loc
        async def evaluate(self, script):
            return 1
    asyncio.run(teams_bridge._back_to_chat(P()))
    assert clicked == ['button[aria-label^="Chat"]:visible']


def test_a_stale_pool_records_why(monkeypatch):
    import time as _t

    class P:
        async def evaluate(self, *a):
            raise RuntimeError("Target page, context or browser has been closed")
    monkeypatch.setitem(teams_bridge._POOL, "page", P())
    monkeypatch.setitem(teams_bridge._POOL, "born", _t.time())
    monkeypatch.setattr(teams_bridge, "_too_big", lambda: False)
    teams_bridge._WHY["discard"] = ""
    assert asyncio.run(teams_bridge._pool_alive()) is False
    assert "has been closed" in teams_bridge._WHY["discard"]
    teams_bridge._POOL.clear()


def test_an_unreadable_feed_does_not_look_like_no_mentions(monkeypatch):
    page = _Page(read_result={"valid": False, "rows": []})
    discarded = []

    async def pooled():
        return page

    async def discard(why=""):
        discarded.append(why)

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    monkeypatch.setattr(teams_bridge, "_discard_pool", discard)
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    with pytest.raises(teams_bridge.ActivityUnavailable, match="rows could not be read"):
        asyncio.run(teams_bridge.read_activity_rows())
    assert store.kv_get("teams_session_ok") != "1"
    assert page.navigated == [teams_bridge.TEAMS_URL] and not discarded


def test_a_confirmed_empty_feed_is_a_success(monkeypatch):
    page = _Page(read_result={"valid": True, "rows": []})

    async def pooled():
        return page

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    assert asyncio.run(teams_bridge.read_activity_rows()) == []
    assert page.chat and store.kv_get("teams_session_ok") == "1"


def test_a_failed_return_to_chat_discards_the_page(monkeypatch):
    page = _Page(restore_fails=True)
    discarded = []

    async def pooled():
        return page

    async def discard(why=""):
        discarded.append(why)

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    monkeypatch.setattr(teams_bridge, "_discard_pool", discard)
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    with pytest.raises(RuntimeError, match="Teams did not reload"):
        asyncio.run(teams_bridge.read_activity_rows())
    assert discarded and store.kv_get("teams_session_ok") != "1"


def test_a_failed_chat_click_can_reload_without_losing_the_activity_read(monkeypatch):
    page = _Page()
    page.restore_fails = True
    async def pooled():
        return page
    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)

    async def reloads(url, **kwargs):
        page.navigated.append(url)
        page.chat = True
    page.goto = reloads
    rows = asyncio.run(teams_bridge.read_activity_rows())
    assert rows[0]["text"] == "Sam mentioned you"
    assert page.navigated == [teams_bridge.TEAMS_URL] and page.chat


def test_a_false_offline_signal_still_reads_the_activity_feed(monkeypatch):
    offline = _Page(state={"offline": True, "network": True})
    discarded = []

    async def pooled():
        return offline

    async def discard(why=""):
        discarded.append(why)

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    monkeypatch.setattr(teams_bridge, "_discard_pool", discard)
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    assert asyncio.run(teams_bridge.read_activity_rows())[0]["text"] == "Sam mentioned you"
    assert offline.clicks == 1 and offline.probes == 1 and not discarded
    assert offline.chat and store.kv_get("teams_session_ok") == "1"


def test_a_cached_feed_during_real_offline_is_not_counted_as_a_read(monkeypatch):
    offline = _Page(state={"offline": True, "network": False})
    discarded = []

    async def pooled():
        return offline

    async def discard(why=""):
        discarded.append(why)

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    monkeypatch.setattr(teams_bridge, "_discard_pool", discard)
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    with pytest.raises(teams_bridge.ActivityOffline, match="visible feed may be cached"):
        asyncio.run(teams_bridge.read_activity_rows())
    assert offline.clicks == 1 and offline.probes == 1 and not discarded
    assert offline.chat and store.kv_get("teams_session_ok") != "1"


def test_a_login_redirect_does_not_verify_cached_activity(monkeypatch):
    page = _Page(state={"offline": True, "network": True,
                        "network_url": "https://login.microsoftonline.com/"})
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)

    async def pooled():
        return page

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    with pytest.raises(teams_bridge.ActivityOffline):
        asyncio.run(teams_bridge.read_activity_rows())
    assert page.probes == 1


def test_a_redirect_from_teams_is_still_teams_answering():
    """Its front page answers 302; that is not "offline" (7 Oct, all evening)."""
    page = _Page(state={"offline": True, "network": False, "status": 302})
    assert asyncio.run(teams_bridge._activity_connected(page)) is True


def test_connectivity_probe_never_calls_a_non_teams_origin():
    page = _Page(state={"offline": True, "network": True})
    page.url = "https://other.example.invalid/"
    assert asyncio.run(teams_bridge._activity_connected(page)) is False
    assert page.probes == 0


def test_activity_catches_up_past_the_first_25_rows(monkeypatch):
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    old = {"text": "Previously seen mention", "unread": True}
    store.kv_set(teams_bridge.ACTIVITY_SEEN_KEY,
                 json.dumps([teams_bridge._activity_key(old["text"])]))
    first = [{"text": f"New mention {i}", "unread": True} for i in range(25)]
    page = _Page(read_result={"valid": True, "rows": first})
    original = page.evaluate
    scrolled = False

    async def evaluate(script, *args):
        nonlocal scrolled
        if script == teams_bridge._ACTIVITY_SCROLL_JS:
            scrolled = True
            return True
        if script == teams_bridge._ACTIVITY_ROWS_JS and scrolled:
            return {"valid": True, "rows": [first[-1], {"text": "New mention 25", "unread": True}, old]}
        return await original(script, *args)

    page.evaluate = evaluate

    async def pooled():
        return page

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    rows = asyncio.run(teams_bridge.read_activity_rows(limit=200))
    assert len(rows) == 27 and rows.complete
    assert rows[-1] == old
    assert store.kv_get(teams_bridge.ACTIVITY_BACKLOG_KEY) == ""


def test_unscannable_activity_backlog_is_reported_not_silently_cleared(monkeypatch):
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    store.kv_set(teams_bridge.ACTIVITY_SEEN_KEY, json.dumps(["older-known-row"]))
    page = _Page(read_result={"valid": True, "rows": [
        {"text": f"New mention {i}", "unread": True} for i in range(25)]})
    original = page.evaluate

    async def evaluate(script, *args):
        if script == teams_bridge._ACTIVITY_SCROLL_JS:
            return False
        return await original(script, *args)

    page.evaluate = evaluate

    async def pooled():
        return page

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    rows = asyncio.run(teams_bridge.read_activity_rows(limit=200))
    assert len(rows) == 25 and not rows.complete
    assert store.kv_get(teams_bridge.ACTIVITY_BACKLOG_KEY) == '["older-known-row"]'
    assert any(r["outcome"] == "activity_backlog_incomplete"
               for r in store.recent_outcomes(5))


def test_a_broken_app_shell_is_not_kept_as_a_healthy_browser(monkeypatch):
    page = _Page(state={"app": False})
    discarded = []

    async def pooled():
        return page

    async def discard(why=""):
        discarded.append(why)

    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)
    monkeypatch.setattr(teams_bridge, "_discard_pool", discard)
    monkeypatch.setattr(teams_bridge.asyncio, "sleep", _instant)
    with pytest.raises(RuntimeError, match="app shell not ready"):
        asyncio.run(teams_bridge.read_activity_rows())
    assert discarded and not page.navigated


def test_failed_activity_repair_must_recheck_the_feed_not_just_login(monkeypatch):
    checked = []

    async def close():
        return None

    async def unreadable():
        checked.append("feed")
        raise teams_bridge.ActivityUnavailable("feed selector changed")

    monkeypatch.setattr(teams_bridge, "close_pool", close)
    monkeypatch.setattr(teams_bridge, "reap_orphans", lambda: None)
    monkeypatch.setattr(teams_bridge, "read_activity_rows", unreadable)
    async def go():
        for name, attempt in teams_bridge.repair_rungs()[:2]:
            assert name in ("recycle", "restart")
            with pytest.raises(teams_bridge.ActivityUnavailable):
                await attempt()
    asyncio.run(go())
    assert checked == ["feed", "feed"]


@pytest.mark.parametrize("failure, expected_rungs", [
    (teams_bridge.ActivityUnavailable("feed selector changed"), ["retry_activity"]),
    (teams_bridge.ActivityOffline("Teams is offline"), ["retry_activity"]),
    (RuntimeError("Teams app shell not ready"), ["recycle", "restart"]),
])
def test_activity_failures_never_trigger_profile_repair(monkeypatch, failure, expected_rungs):
    from app import recovery
    calls, attempts = [], []

    class Finished(BaseException):
        pass

    async def tick(seconds):
        if len(attempts) == 3:
            raise Finished
        attempts.append(seconds)

    async def fail_read(limit=25):
        raise failure

    async def ladder(source, rungs, stale_polls, **kwargs):
        calls.append((stale_polls, [name for name, _ in rungs]))
        if stale_polls == 3 and isinstance(failure, teams_bridge.ActivityUnavailable):
            with pytest.raises(teams_bridge.ActivityUnavailable):
                await rungs[0][1]()
        return {"healed": False}

    monkeypatch.setattr(teams_bridge, "_activity_wait", tick)
    monkeypatch.setattr(teams_bridge, "read_activity_rows", fail_read)
    monkeypatch.setattr(teams_bridge, "reap_orphans", lambda: None)
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)
    monkeypatch.setattr(recovery, "ladder", ladder)
    with pytest.raises(Finished):
        asyncio.run(teams_bridge.activity_watch_loop())
    assert calls == [(1, expected_rungs), (2, expected_rungs), (3, expected_rungs)]


def test_confirmed_sign_in_stops_polls_and_reports_relogin(monkeypatch):
    from app import notify, recovery
    sent, checked = [], []

    class Finished(BaseException):
        pass

    async def tick(seconds):
        if checked:
            raise Finished

    async def fail_read(limit=25):
        raise RuntimeError("SESSION_EXPIRED: Teams is showing a sign-in page")

    async def check():
        checked.append(True)
        store.kv_set("teams_session_ok", "0")
        return False

    async def report(text, *args, **kwargs):
        sent.append(text)

    async def no_repair(*args, **kwargs):
        pytest.fail("expired SSO should not repair Teams' profile")

    monkeypatch.setattr(teams_bridge, "_activity_wait", tick)
    monkeypatch.setattr(teams_bridge, "read_activity_rows", fail_read)
    monkeypatch.setattr(teams_bridge, "check_session", check)
    monkeypatch.setattr(teams_bridge, "reap_orphans", lambda: None)
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)
    monkeypatch.setattr(notify, "notify", report)
    monkeypatch.setattr(recovery, "ladder", no_repair)
    with pytest.raises(Finished):
        asyncio.run(teams_bridge.activity_watch_loop())
    assert checked == [True] and len(sent) == 1 and "session expired" in sent[0]


def test_a_repaired_activity_feed_is_read_again_without_waiting_for_the_next_poll(monkeypatch):
    from app import recovery
    reads = []

    class Finished(BaseException):
        pass

    async def tick(seconds):
        if len(reads) == 1:
            assert teams_bridge.mentioned().is_set()
        if len(reads) == 2:
            raise Finished

    async def read(limit=25):
        reads.append(True)
        if len(reads) == 1:
            raise teams_bridge.ActivityUnavailable("transient feed failure")
        return []

    async def repaired(*args, **kwargs):
        return {"healed": True}

    monkeypatch.setitem(teams_bridge.MENTIONED, "ev", None)
    monkeypatch.setattr(teams_bridge, "_activity_wait", tick)
    monkeypatch.setattr(teams_bridge, "read_activity_rows", read)
    monkeypatch.setattr(teams_bridge, "reap_orphans", lambda: None)
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)
    monkeypatch.setattr(recovery, "ladder", repaired)
    with pytest.raises(Finished):
        asyncio.run(teams_bridge.activity_watch_loop())
    assert len(reads) == 2


def test_activity_recovery_never_touches_a_live_call(monkeypatch):
    ticks = []

    class Finished(BaseException):
        pass

    async def tick(seconds):
        if ticks:
            raise Finished
        ticks.append(True)

    async def unexpected_read():
        pytest.fail("the call owns the browser profile")

    monkeypatch.setattr(teams_bridge, "_activity_wait", tick)
    monkeypatch.setattr(teams_bridge, "read_activity_rows", unexpected_read)
    monkeypatch.setattr(teams_bridge, "reap_orphans", lambda: None)
    monkeypatch.setattr(teams_bridge, "in_a_call", lambda: True)
    with pytest.raises(Finished):
        asyncio.run(teams_bridge.activity_watch_loop())


@pytest.mark.asyncio
async def test_activity_reader_distinguishes_real_rows_empty_feed_and_broken_markup():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"no isolated Chromium available: {exc}")
        try:
            page = await browser.new_page()
            await page.set_content("""<div data-tid="activity-list-container">
                <div role="listbox"><div role="option" aria-label="Unread">
                <div>Sam</div><div>mentioned you in a channel</div></div></div></div>""")
            got = await page.evaluate(teams_bridge._ACTIVITY_ROWS_JS)
            assert got == {"valid": True, "rows": [
                {"text": "Sam — mentioned you in a channel", "unread": True}]}

            await page.set_content("""<div data-tid="activity-list-container">
                You're all caught up</div>""")
            assert await page.evaluate(teams_bridge._ACTIVITY_ROWS_JS) == {
                "valid": True, "rows": []}

            await page.set_content('<div data-tid="activity-list-container"></div>')
            assert (await page.evaluate(teams_bridge._ACTIVITY_ROWS_JS))["valid"] is False

            await page.set_content("""<div data-tid="activity-list-container"></div>
                <div data-tid="activity-feed-list-item" style="display:none">
                <div>Old mention</div><div>already gone</div></div>""")
            assert (await page.evaluate(teams_bridge._ACTIVITY_ROWS_JS))["valid"] is False

            await page.set_content("""<div data-tid="activity-list-container"></div>
                <div role="listbox"><div role="option"><div>Sam</div>
                <div>mentioned you in a channel</div></div></div>""")
            got = await page.evaluate(teams_bridge._ACTIVITY_ROWS_JS)
            assert got["valid"] and got["rows"][0]["text"].startswith("Sam —")

            await page.set_content("""<div data-tid="activity-feed-list-item">
                <div>Sam</div><div>mentioned you in a channel</div></div>""")
            assert (await page.evaluate(teams_bridge._ACTIVITY_ROWS_JS))["valid"]

            await page.set_content("""<div data-tid="app-bar">Welcome to Teams.
                You can sign in to another app.</div>
                <button aria-label="Activity (Ctrl Shift 1)">Activity</button>""")
            state = await teams_bridge._page_state(page)
            assert state["app"] and state["button"] and not state["signin"]

            await page.set_content("<h1>Pick an account</h1>")
            state = await teams_bridge._page_state(page)
            assert state["signin"] and not state["app"]

            await page.set_content("<div>Oops, app failed to load</div>")
            assert (await teams_bridge._page_state(page))["oops"]

            await page.set_content("""<div data-tid="activity-list-container"
                style="height:40px;overflow-y:auto">
                <div role="listbox"><div style="height:300px">Older activity</div></div>
                </div>""")
            assert await page.evaluate(teams_bridge._ACTIVITY_SCROLL_JS)
        finally:
            await browser.close()
