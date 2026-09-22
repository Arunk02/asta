"""Quiet time and not telling him twice — Round 3, P8.

19 Sep: he said Saturday and Sunday are his days off, nothing should notify
him, and everything should be summarised on Monday morning. The brain said
"Saved", and thirteen notifications followed over the weekend: the rule had
nowhere to live, and the digest, the batch flush and the held flush all went to
his phone around the checks `notify` made.

tests/workworld/quiet.yaml proves the behaviour end to end; these pin the door.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import attention, digest, notify, policy, store


@pytest.fixture
def phone(monkeypatch):
    """What reached his phone, in order."""
    sent: list[str] = []

    async def wa(text):
        sent.append(text)
        return True

    async def tg(text, *a, **k):
        return False

    monkeypatch.setattr(notify, "wa_send", wa)
    monkeypatch.setattr(notify.telegram, "send", tg)
    return sent


def _quiet_for(seconds: float, *, started: float = 60) -> None:
    now = time.time()
    policy.add("quiet", "push", value=f"from={now - started};until={now + seconds}",
               words="quiet for a while")


def _at_laptop(monkeypatch, here: bool = True) -> None:
    from app import presence

    async def at():
        return here

    monkeypatch.setattr(presence, "at_laptop", at)


# --- one door --------------------------------------------------------------------

def test_a_quiet_rule_holds_every_way_to_his_phone(phone):
    _quiet_for(3600)
    asyncio.run(notify.notify("🔴 CI failure: booking-service", "ci", urgency="direct"))
    asyncio.run(notify.deliver("📥 Digest — 3 things"))            # the digest's path
    store.kv_set(notify.HELD_KEY, '[{"at": 1, "text": "held ambient thing"}]')
    asyncio.run(notify.flush_held())                                # the held flush
    assert phone == []
    kept = [r["text"] for r in notify._items(notify.QUIET_KEY)]
    assert "🔴 CI failure: booking-service" in kept and "📥 Digest — 3 things" in kept


def test_his_own_reminder_rings_through_quiet_time(phone):
    _quiet_for(3600)
    out = asyncio.run(notify.notify("⏰ Reminder: call the plumber", "reminder", asked=True))
    assert phone == ["⏰ Reminder: call the plumber"] and out["whatsapp"]


def test_when_the_quiet_ends_everything_arrives_as_one_summary(phone):
    _quiet_for(3600)
    asyncio.run(notify.notify("can you check the build?", "teams-chat"))
    asyncio.run(notify.notify("Grafana alert (firing): memory above 85%", "outlook"))
    assert asyncio.run(notify.release_quiet())["sent"] is False     # still quiet
    later = time.time() + 7200
    out = asyncio.run(notify.release_quiet(later))
    assert out["sent"] and len(phone) == 1
    assert "While you were off" in phone[0]
    assert "check the build" in phone[0] and "memory above 85" in phone[0]
    assert asyncio.run(notify.release_quiet(later))["sent"] is False  # said once


def test_no_digest_slot_fires_while_quiet(phone):
    midday = time.time()
    policy.add("quiet", "push", value=f"from={midday - 60};until={midday + 3600}")
    digest.add("something quiet", source="ci")
    out = asyncio.run(digest.tick(now=midday))
    assert out["sent"] is False and phone == []
    assert [r["text"] for r in digest.pending()] == ["something quiet"]


# --- not telling him twice ---------------------------------------------------------

def _owed(key: str, who: str = "A colleague") -> None:
    attention.consider("teams-chat", key, who=who, what=f"{who}: can you check?",
                       priority=attention.P_TODAY)


def test_a_teams_message_he_answers_never_reaches_his_phone(phone, monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    _at_laptop(monkeypatch)
    _owed("k-lag")
    out = asyncio.run(notify.notify("💬 Teams\n🔴 A colleague: consumer lag?", "teams",
                                    considered=True, keys=("k-lag",)))
    assert out.get("grace") and phone == []
    attention.mark_acted("k-lag", why="he replied")
    assert asyncio.run(notify.release_grace(time.time() + notify.REPLY_GRACE + 1)) == 0
    assert phone == [] and notify._items(notify.GRACE_KEY) == []


def test_an_unanswered_one_still_goes_after_the_grace(phone, monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    _at_laptop(monkeypatch)
    _owed("k-lag")
    asyncio.run(notify.notify("💬 Teams\n🔴 A colleague: consumer lag?", "teams",
                              considered=True, keys=("k-lag",)))
    assert asyncio.run(notify.release_grace(time.time() + 5)) == 0      # still waiting
    assert asyncio.run(notify.release_grace(time.time() + notify.REPLY_GRACE + 1)) == 1
    assert phone == ["💬 Teams\n🔴 A colleague: consumer lag?"]


def test_his_reply_in_the_stored_thread_counts_as_an_answer(monkeypatch):
    """Nothing else settles an ask he answered by typing in Teams himself: the
    watcher skips his own messages, and they only land in the stored thread."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    _owed("k-doc", who="A colleague")
    later = time.time() + 30
    store.save_teams_messages([{"key": "m1", "chat": "A colleague", "sender": "Arun K",
                                "text": "done, check now", "sent_at": later, "stamp": ""}])
    assert notify.answered(("k-doc",))
    assert store.attention_get("k-doc")["state"] == "acted"


def test_a_push_with_nothing_behind_it_is_never_answered():
    assert not notify.answered(())
    assert not notify.answered(("no-such-key",))


def test_the_digest_leaves_out_what_he_has_answered(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    _owed("k-a")
    _owed("k-b", who="Another colleague")
    digest.add("I merged the doc update", source="teams-chat", keys=("k-a",))
    digest.add("shared the release notes", source="teams-chat", keys=("k-b",))
    attention.mark_acted("k-a", why="he replied")
    assert [r["text"] for r in digest.take()] == ["shared the release notes"]


def test_reminders_are_his_own_ask():
    """Pinned at the call site: the exemption is worthless if the reminder loop
    does not claim it."""
    import inspect
    from app import reminders
    assert "asked=True" in inspect.getsource(reminders.fire_due)


# --- what he says, and the window it means ------------------------------------------

_TUESDAY = time.mktime(time.strptime("2026-09-15 10:00", "%Y-%m-%d %H:%M"))
_SUNDAY = time.mktime(time.strptime("2026-09-20 10:00", "%Y-%m-%d %H:%M"))


def _window(text: str, now: float = _TUESDAY) -> str:
    from app import instructions
    spec = instructions.quiet_spec(text, now)
    q = policy.parse_quiet(spec) if spec else {}
    if "until" not in q:
        return spec
    f = lambda x: time.strftime("%a %d %H:%M", time.localtime(x))   # noqa: E731
    return f"{f(q['from'])} → {f(q['until'])}"


@pytest.mark.parametrize("said, window", [
    # his own, from 19 Sep — every week, released Monday morning
    ("And also sat and Sunday be on silent since it is off leave for me. So don't have to "
     "notify me anything .. if there is anything summarise all on Monday mrng .. follow the "
     "same going forward", "days=sat,sun;release=mon 09:00"),
    # his own, from 15 Sep — one window each, which ends by itself
    ("go silent for a week, don't notify me anything", "Tue 15 10:00 → Tue 22 09:00"),
    ("don't notify me anything until 11:30", "Tue 15 10:00 → Tue 15 11:30"),
    ("don't notify me anything until 9", "Tue 15 10:00 → Wed 16 09:00"),
    ("no notifications till 5pm", "Tue 15 10:00 → Tue 15 17:00"),
    ("be on silent until tomorrow 11:30", "Tue 15 10:00 → Wed 16 11:30"),
    ("stay quiet until monday", "Tue 15 10:00 → Mon 21 09:00"),
    ("mute everything this weekend", "Sat 19 00:00 → Mon 21 09:00"),
    ("don't ping me on sunday", "Sun 20 00:00 → Mon 21 09:00"),
    ("don't ping me on sundays", "days=sun;release=mon 09:00"),
    ("stay quiet tomorrow", "Wed 16 00:00 → Thu 17 09:00"),
    ("go quiet for 2 hours", "Tue 15 10:00 → Tue 15 12:00"),
    ("dnd for the rest of the day", "Tue 15 10:00 → Wed 16 09:00"),
    # not quiet time at all
    ("please check the build on saturday", ""),
    ("don't message Sam on sunday", ""),          # a rule about Sam, not his phone
    ("don't push to main on friday", ""),
    ("I sat at my desk, the sun was out", ""),
    ("it was a quiet sunday", ""),
])
def test_what_he_says_becomes_the_window_he_means(said, window):
    assert _window(said) == window


def test_this_weekend_said_on_a_sunday_is_today():
    assert _window("mute everything this weekend", _SUNDAY) == "Sun 20 10:00 → Mon 21 09:00"


def test_a_one_off_window_leaves_his_rules_once_it_has_ended(phone):
    _quiet_for(60, started=600)
    assert policy.rules("quiet")
    asyncio.run(notify.release_quiet(time.time() + 120))
    assert policy.rules("quiet") == []


# --- a brain that files his instruction as a memory ---------------------------------

def test_a_brain_filing_his_weekend_rule_as_a_memory_offers_the_rule_instead(monkeypatch):
    from app import agent, memory

    def no_note(*a, **k):
        raise AssertionError("filed as a memory note, which nothing sending a push reads")

    monkeypatch.setattr(memory, "remember", no_note)
    out = agent.remember("standing-weekend-silence",
                         "Saturday and Sunday are his days off. Don't notify him anything; "
                         "summarise everything on Monday morning. Follow the same going forward.")
    assert "Make this a standing rule" in out and "Quiet on Saturdays and Sundays" in out
    # Asked twice (the front desk already offered it, or the brain retries): not re-offered.
    again = agent.remember("weekend", "Don't notify him on Saturdays and Sundays, going forward")
    assert "already offered" in again


def test_an_ordinary_fact_is_still_remembered(monkeypatch):
    from app import agent, memory
    monkeypatch.setattr(memory, "remember", lambda title, fact, kind: "memory/facts/x.md")
    assert agent.remember("deploys", "The deploy pipeline is slow on Mondays") == \
        "Remembered in memory/facts/x.md"


def test_a_week_of_quiet_is_not_a_week_of_him_ignoring_things(monkeypatch, phone):
    """The hourly sweep drops anything unanswered for seven days as "ignored".
    A week-long quiet window is exactly when he was not there to answer."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    _quiet_for(3600)
    _owed("k-week")
    asyncio.run(notify.notify("💬 A colleague: can you check the release?", "teams-chat",
                              considered=True, keys=("k-week",)))
    store.attention_set("k-week", state="dropped")          # what settle_stale does
    asyncio.run(notify.release_quiet(time.time() + 7200))
    assert phone and "check the release" in phone[0]


def test_on_a_quiet_day_what_he_asks_asta_for_still_reaches_him(phone):
    """He can still ask Asta for something on a Saturday; the answer must not
    wait for Monday. A colleague's message still does."""
    _quiet_for(3600)
    store.add_ui_message("wa", "user", "can you check why the build is red?")
    asyncio.run(notify.notify("✅ Task #12 finished: the build is green again", "task"))
    asyncio.run(notify.notify("💬 A colleague: are you around?", "teams-chat"))
    assert phone == ["✅ Task #12 finished: the build is green again"]
