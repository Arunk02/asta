"""When to say it: not at 3am, not four times, and not never.

The delivery layer is the one place a mistake is silent — an over-eager hold
looks exactly like nothing happening. So most of these tests are about what
still gets through: breakage at night, calls whose rank Asta does not understand,
and anything he set himself.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app import attention, delivery, notify, store


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ASTA_DELIVERY", "1")
    monkeypatch.setenv("ASTA_QUIET_HOURS", "22:00-07:00")


def _at(hour: int, minute: int = 0) -> float:
    return dt.datetime(2026, 6, 4, hour, minute).timestamp()


@pytest.fixture(autouse=True)
def _no_channels(monkeypatch):
    """Nothing here may reach a real phone; record what would have been sent."""
    sent: list[str] = []

    async def _wa(text):
        sent.append(text)
        return True

    async def _tg(text):
        return True

    monkeypatch.setattr(notify, "wa_send", _wa)
    monkeypatch.setattr(notify.telegram, "send", _tg)
    return sent


# --- quiet hours ------------------------------------------------------------------

def test_no_window_configured_means_never_quiet(monkeypatch):
    monkeypatch.delenv("ASTA_QUIET_HOURS", raising=False)
    assert delivery.quiet_window() is None
    assert delivery.in_quiet_hours(_at(3)) is False


def test_a_typo_in_the_window_disables_it_rather_than_guessing(monkeypatch):
    """Inventing a default would mute him on a config typo, and being wrongly
    silent is the exact failure this subsystem exists to avoid."""
    monkeypatch.setenv("ASTA_QUIET_HOURS", "10pm to 7am")
    assert delivery.quiet_window() is None
    assert delivery.in_quiet_hours(_at(3)) is False


def test_a_window_that_wraps_midnight_is_the_normal_case(on):
    assert delivery.in_quiet_hours(_at(23)) is True
    assert delivery.in_quiet_hours(_at(3)) is True
    assert delivery.in_quiet_hours(_at(6, 59)) is True
    assert delivery.in_quiet_hours(_at(7)) is False
    assert delivery.in_quiet_hours(_at(14)) is False


def test_a_daytime_window_also_works(monkeypatch):
    monkeypatch.setenv("ASTA_DELIVERY", "1")
    monkeypatch.setenv("ASTA_QUIET_HOURS", "09:00-11:00")
    assert delivery.in_quiet_hours(_at(10)) is True
    assert delivery.in_quiet_hours(_at(23)) is False


def test_an_l2_ticket_can_wait_until_morning(on):
    assert delivery.hold_for_quiet("direct", attention.P_TODAY, _at(2)) is True


def test_breakage_still_earns_the_night(on):
    assert delivery.hold_for_quiet("direct", attention.P_NOW, _at(2)) is False


def test_a_call_whose_rank_is_unknown_is_never_silenced(on):
    """A reminder he set, a task finishing, a question Asta is asking. Silencing
    calls whose meaning is not understood is how quiet hours starts eating things
    that mattered."""
    assert delivery.hold_for_quiet("direct", None, _at(2)) is False


def test_ambient_chatter_is_held_at_night_even_though_he_is_away(on):
    assert delivery.hold_for_quiet("ambient", None, _at(2)) is True


def test_nothing_is_held_during_the_day(on):
    assert delivery.hold_for_quiet("direct", attention.P_TODAY, _at(14)) is False
    assert delivery.hold_for_quiet("ambient", None, _at(14)) is False


def test_disabled_holds_nothing(monkeypatch):
    monkeypatch.delenv("ASTA_DELIVERY", raising=False)
    monkeypatch.setenv("ASTA_QUIET_HOURS", "22:00-07:00")
    assert delivery.hold_for_quiet("direct", attention.P_TODAY, _at(2)) is False


# --- the hold must not release the moment he goes to bed ---------------------------

def test_held_items_are_not_flushed_during_quiet_hours(on, monkeypatch, _no_channels):
    """The subtle one. The held queue releases when he steps AWAY — and at 2am he
    has already stepped away, so without this guard a quiet-hours hold would fire
    instantly, achieving precisely nothing."""
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: True)
    notify._hold("something that can wait")
    asyncio.run(notify.flush_held())
    assert _no_channels == []
    assert len(notify._held_items()) == 1


def test_held_items_go_out_once_the_window_has_passed(on, monkeypatch, _no_channels):
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: False)
    notify._hold("morning news")
    asyncio.run(notify.flush_held())
    assert _no_channels and "morning news" in _no_channels[0]
    assert notify._held_items() == []


def test_a_nighttime_push_lands_in_the_hold_rather_than_on_his_phone(on, monkeypatch,
                                                                    _no_channels):
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: True)
    out = asyncio.run(notify.notify("L2 ticket assigned", "outlook",
                                    urgency="direct", priority=attention.P_TODAY))
    assert out["held"] is True and _no_channels == []
    assert store.list_notifications()          # the bell still has it


def test_a_nighttime_outage_goes_straight_through(on, monkeypatch, _no_channels):
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: True)
    out = asyncio.run(notify.notify("pods are down", "outlook",
                                    urgency="direct", priority=attention.P_NOW))
    assert out["held"] is False and _no_channels == ["pods are down"]


# --- one message instead of four ----------------------------------------------------

def test_nothing_is_batched_when_nothing_went_out_recently(on):
    assert delivery.should_batch(attention.P_TODAY, _at(14)) is False


def test_something_arriving_moments_later_rides_along(on):
    delivery.note_sent(_at(14))
    assert delivery.should_batch(attention.P_TODAY, _at(14, 1)) is True


def test_the_window_closes(on):
    delivery.note_sent(_at(14))
    assert delivery.should_batch(attention.P_TODAY, _at(14, 5)) is False


def test_breakage_never_waits_for_a_batch(on):
    delivery.note_sent(_at(14))
    assert delivery.should_batch(attention.P_NOW, _at(14, 1)) is False


def test_batching_can_be_switched_off(on, monkeypatch):
    monkeypatch.setenv("ASTA_COALESCE_SECONDS", "0")
    delivery.note_sent(_at(14))
    assert delivery.should_batch(attention.P_TODAY, _at(14, 1)) is False


def test_a_batched_push_is_buffered_not_dropped(on, monkeypatch, _no_channels):
    monkeypatch.setattr(delivery, "should_batch", lambda *a, **k: True)
    out = asyncio.run(notify.notify("second thing", "outlook", urgency="direct"))
    assert out.get("batched") is True and _no_channels == []
    assert delivery.take_buffered() == ["second thing"]


def test_the_buffer_is_rendered_as_one_message(on):
    assert delivery.render_batch(["a"]) == "a"
    merged = delivery.render_batch(["a", "b", "c"])
    assert merged.startswith("📬 3 updates:") and "a" in merged and "c" in merged


def test_delivering_stamps_the_clock_so_the_next_one_can_batch(on, _no_channels):
    asyncio.run(notify.deliver("first"))
    assert delivery.should_batch(attention.P_TODAY) is True


# --- chasing what he never answered --------------------------------------------------

def _owed(key: str, priority: int = attention.P_TODAY, due: float | None = None):
    store.attention_upsert(key, "outlook", who="Sam", what=f"Sam: {key}", priority=priority)
    store.attention_set(key, state="notified", notified_at=_at(9), due_at=due)


def test_something_past_its_deadline_is_chased(on):
    _owed("approve-the-release", due=_at(15))
    assert [r["key"] for r in delivery.chase_due(_at(16))] == ["approve-the-release"]


def test_something_still_inside_its_deadline_is_left_alone(on):
    _owed("approve-the-release", due=_at(17))
    assert delivery.chase_due(_at(16)) == []


def test_something_with_no_deadline_is_chased_at_the_end_of_the_day(on):
    _owed("look-at-this")
    assert delivery.chase_due(_at(12)) == []
    assert [r["key"] for r in delivery.chase_due(_at(18, 30))] == ["look-at-this"]


def test_pure_fyi_is_never_chased(on):
    _owed("newsletter", priority=attention.P_FYI)
    assert delivery.chase_due(_at(19)) == []


def test_something_he_dealt_with_is_never_chased(on):
    _owed("approve-the-release", due=_at(15))
    attention.mark_acted("approve-the-release")
    assert delivery.chase_due(_at(16)) == []


def test_it_chases_once_and_then_stops(on):
    """A second automated nudge is nagging, and an assistant that nags gets muted
    — which costs him the first nudge too."""
    _owed("approve-the-release", due=_at(15))
    rows = delivery.chase_due(_at(16))
    delivery.mark_chased(rows, _at(16))
    assert delivery.chase_due(_at(20)) == []


def test_the_chase_says_what_is_owed_and_to_whom(on):
    _owed("approve-the-release", due=_at(15))
    text = delivery.render_chase(delivery.chase_due(_at(16)))
    assert "Still waiting on you" in text and "Sam" in text


def test_a_flush_that_reached_nobody_keeps_the_batch_and_says_so(on, monkeypatch):
    """Held items are the ones Asta deliberately kept back, so they are the only
    copy. The queue was cleared BEFORE the send and both results discarded, so a
    flush with WhatsApp unpaired and Telegram unbound deleted the batch and
    reported success to a caller that then told him it was delivered."""
    async def _down(text):
        return False

    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: False)
    monkeypatch.setattr(notify, "wa_send", _down)
    monkeypatch.setattr(notify.telegram, "send", _down)

    notify._hold("something worth keeping")
    out = asyncio.run(notify.flush_held())

    assert out == {"held": True, "whatsapp": False, "telegram": False}
    assert [i["text"] for i in notify._held_items()] == ["something worth keeping"]
    assert store.kv_get("last_push_failure")


def test_a_flush_that_landed_reports_the_channels_that_took_it(on, monkeypatch,
                                                               _no_channels):
    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: False)
    notify._hold("news")
    out = asyncio.run(notify.flush_held())
    assert out["held"] is False and out["whatsapp"] is True
    assert notify._held_items() == []


def test_the_stale_release_no_longer_claims_a_delivery_it_never_checked(on, monkeypatch):
    async def _down(text):
        return False

    monkeypatch.setattr(delivery, "in_quiet_hours", lambda now=None: False)
    monkeypatch.setattr(delivery, "hold_for_quiet", lambda *a, **k: False)
    monkeypatch.setattr(notify, "wa_send", _down)
    monkeypatch.setattr(notify.telegram, "send", _down)
    monkeypatch.setattr(notify, "_stale", lambda items, now=None: True)

    async def _away():
        return True

    from app import presence
    monkeypatch.setattr(presence, "at_laptop", _away)

    out = asyncio.run(notify.notify("ambient thing", "outlook", urgency="ambient"))
    assert out["whatsapp"] is False and out["telegram"] is False


def test_disabled_nothing_changes_at_all(monkeypatch, _no_channels):
    monkeypatch.delenv("ASTA_DELIVERY", raising=False)
    monkeypatch.setenv("ASTA_QUIET_HOURS", "22:00-07:00")
    out = asyncio.run(notify.notify("anything", "outlook", urgency="direct",
                                    priority=attention.P_TODAY))
    assert out["held"] is False and _no_channels == ["anything"]
