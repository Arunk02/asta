"""A message read just before a restart is still dealt with after it.

28 Oct: a colleague's "Hellooo !" and a follow-up were read, and the sweep was
still working out what they meant when Asta restarted. Every later sweep called
the chat "0 new": no acknowledgement to them, nothing to him. (Names synthetic.)
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import chat_watch, store, understand
from tests.test_conversation_threads import _msg, rail  # noqa: F401

OPENER = {"state": "opener", "need": "", "closing_confidence": 0.0,
          "summary": "wants a chat", "entities": []}


def killed_once(monkeypatch):
    real = understand.read
    calls = {"n": 0}

    async def read(convs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.CancelledError()          # the restart, mid-understanding
        return await real(convs)

    monkeypatch.setattr(understand, "read", read)


def test_a_message_read_before_a_restart_is_acknowledged_after_it(rail, monkeypatch):
    rail.rows["Priya S"] = [_msg("Priya S", "Hellooo !"), _msg("Priya S", "you there ?")]
    rail.script["teams:Priya S"] = OPENER
    killed_once(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        rail.sweep()
    assert rail.sent == [] and "Priya S" not in rail.rows, "read and marked processed, then lost"
    rail.sweep()                                     # the first sweep after the restart
    assert [c for c, _ in rail.sent] == ["Priya S"], "asked what it is about, once"
    assert json.loads(store.kv_get("teams_inflight") or "{}") == {}
    rail.sweep()
    assert len(rail.sent) == 1, "never handled twice"


def test_a_normal_sweep_leaves_nothing_in_flight(rail):
    rail.rows["Priya S"] = [_msg("Priya S", "Hellooo !")]
    rail.script["teams:Priya S"] = OPENER
    rail.sweep()
    assert json.loads(store.kv_get("teams_inflight") or "{}") == {}


def test_a_message_that_keeps_failing_is_given_up_after_three_sweeps(rail, monkeypatch):
    rail.rows["Priya S"] = [_msg("Priya S", "Hellooo !")]

    async def broken(convs):
        raise RuntimeError("model down")

    monkeypatch.setattr(understand, "read", broken)
    failures = 0
    for _ in range(chat_watch.INFLIGHT_TRIES + 2):   # the first sweep, its retries, then one more
        rail.rows.setdefault("Priya S", [])
        try:
            rail.sweep()
        except RuntimeError:
            failures += 1
    assert failures == chat_watch.INFLIGHT_TRIES + 1, "retried, not dropped at the first failure"
    assert json.loads(store.kv_get("teams_inflight") or "{}") == {}, "given up, not wedged"
    with store._connect() as conn:
        dropped = conn.execute("select count(*) from outcomes where kind='chatwatch' "
                               "and outcome='inflight dropped'").fetchone()[0]
    assert dropped == 1


def test_a_message_seen_again_is_not_doubled(rail, monkeypatch):
    m = _msg("Priya S", "Hellooo !")
    rail.rows["Priya S"] = [m]
    rail.script["teams:Priya S"] = OPENER
    killed_once(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        rail.sweep()
    rail.rows["Priya S"] = [m]                       # the reader hands it over again
    seen = []
    real = understand.read

    async def read(convs):
        seen.extend(convs)
        return await real(convs)

    monkeypatch.setattr(understand, "read", read)
    rail.sweep()
    assert sum(c["new"].count("Hellooo !") for c in seen) == 1
