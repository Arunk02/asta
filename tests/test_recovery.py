"""The actuator. Detection has always worked; this is the part that acts.

The night these tests are written for: 26 August, 23:17. The store held the exact
cause, the exact age and the exact subsystem, and nothing happened for thirteen
and a half hours because every path ended in "tell Arun". These pin the
behaviour that ends that — and, just as importantly, the restraint that stops it
becoming a browser relaunched every sixty seconds.
"""

from __future__ import annotations

import pytest

from app import recovery, store


def _rung(name: str, heals: bool, trace: list[str]):
    async def go() -> bool:
        trace.append(name)
        return heals
    return name, go


def _boom(name: str, trace: list[str]):
    async def go() -> bool:
        trace.append(name)
        raise RuntimeError("this rung is itself broken")
    return name, go


@pytest.mark.asyncio
async def test_the_cheapest_rung_that_works_is_the_last_one_tried():
    trace: list[str] = []
    out = await recovery.ladder("teams", [
        _rung("recycle", True, trace),
        _rung("restart", True, trace),
    ], stale_polls=3)
    assert out["healed"] and out["healed_by"] == "recycle"
    # The expensive rung must not run "just in case" — that is how a repair
    # becomes more disruptive than the fault.
    assert trace == ["recycle"]


@pytest.mark.asyncio
async def test_he_is_not_told_when_it_fixed_itself():
    told: list[str] = []

    async def notify(text, level="info"):
        told.append(text)

    out = await recovery.ladder("teams", [_rung("recycle", True, [])],
                                stale_polls=3, notify=notify)
    assert out["told_him"] is False
    assert told == []


@pytest.mark.asyncio
async def test_he_is_told_when_everything_failed_and_the_message_names_what_was_tried():
    told: list[str] = []

    async def notify(text, level="info"):
        told.append(text)

    trace: list[str] = []
    out = await recovery.ladder("outlook", [
        _rung("recycle", False, trace),
        _rung("restart", False, trace),
    ], stale_polls=4, notify=notify)
    assert out["healed"] is False and out["told_him"] is True
    # A report, not an alarm: he can tell at a glance what is left for him.
    assert "recycle, restart" in told[0]


@pytest.mark.asyncio
async def test_a_rung_that_throws_does_not_abort_the_ladder():
    """The delicate repair being too broken to run is exactly when the blunt one
    is needed. An exception here must not skip it."""
    trace: list[str] = []
    out = await recovery.ladder("teams", [
        _boom("restart", trace),
        _rung("repair", True, trace),
    ], stale_polls=3)
    assert out["healed"] and out["healed_by"] == "repair"
    assert trace == ["restart", "repair"]


@pytest.mark.asyncio
async def test_one_bad_poll_is_a_hiccup_not_an_outage():
    """After the lid opens the network has not reassociated and the first poll
    fails. Repairing on that would relaunch a browser every minute."""
    trace: list[str] = []
    out = await recovery.ladder("teams", [_rung("recycle", True, trace)],
                                stale_polls=1, threshold=3)
    assert out["skipped"] == "not stale enough"
    assert trace == []


@pytest.mark.asyncio
async def test_it_will_not_thrash_a_genuinely_broken_subsystem():
    trace: list[str] = []
    first = await recovery.ladder("wa", [_rung("recycle", False, trace)],
                                  stale_polls=5, now=1000.0)
    assert first["attempts"], "the first run must actually try"
    second = await recovery.ladder("wa", [_rung("recycle", False, trace)],
                                   stale_polls=5, now=1000.0 + recovery.COOLDOWN_SECONDS - 1)
    assert second["skipped"] == "cooling down"
    assert len(trace) == 1


@pytest.mark.asyncio
async def test_he_is_told_once_not_every_cycle():
    told: list[str] = []

    async def notify(text, level="info"):
        told.append(text)

    await recovery.ladder("tg", [_rung("recycle", False, [])],
                          stale_polls=5, now=2000.0, notify=notify)
    await recovery.ladder("tg", [_rung("recycle", False, [])],
                          stale_polls=5, now=2000.0 + recovery.COOLDOWN_SECONDS + 1,
                          notify=notify)
    assert len(told) == 1, "a repeated escalation is the noise he complained about"


@pytest.mark.asyncio
async def test_healing_clears_the_escalation_so_the_next_outage_speaks():
    """The subtle one. If the flag stuck after a fix, the NEXT genuine outage
    would repair-fail silently and he would never hear about it."""
    told: list[str] = []

    async def notify(text, level="info"):
        told.append(text)

    await recovery.ladder("mcp", [_rung("recycle", False, [])],
                          stale_polls=5, now=3000.0, notify=notify)
    assert recovery.already_escalated("mcp")
    await recovery.ladder("mcp", [_rung("recycle", True, [])],
                          stale_polls=5, now=3000.0 + recovery.COOLDOWN_SECONDS + 1)
    assert not recovery.already_escalated("mcp")


@pytest.mark.asyncio
async def test_every_attempt_is_recorded_so_self_healing_is_itself_measurable():
    """Silent recovery that silently stops recovering is the same failure in a
    new costume. The only defence is that the healing is measured too."""
    await recovery.ladder("teams", [_rung("recycle", True, [])], stale_polls=3)
    kinds = [r for r in store.recent_outcomes(50) if r["kind"] == "recovery"]
    assert kinds, "a ladder run that leaves no trace cannot be audited"
    assert recovery.health_line().startswith("self-healing:")
