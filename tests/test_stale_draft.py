"""A yes sends the draft that is in front of him now, not one the chat has moved past.

Synthetic: the colleague and the wording are made up.
"""
from __future__ import annotations

import asyncio
import time

from app import chat_watch, loop


def _draft(age=60, channel="teams", to="Priya Nair"):
    return {"kind": "send", "channel": channel, "to": to, "what": "is the call now or later?",
            "_at": time.time() - age}


def _replied(monkeypatch, answer):
    seen = []

    async def since(chat, at):
        seen.append((chat, at))
        return answer
    monkeypatch.setattr(chat_watch, "he_replied_since", since)
    return seen


def test_a_fresh_draft_he_has_not_overtaken_goes(monkeypatch):
    seen = _replied(monkeypatch, False)
    assert asyncio.run(loop.stale_draft(_draft())) == ""
    assert seen and seen[0][0] == "Priya Nair", "the chat was looked at, fresh"


def test_a_draft_he_has_answered_himself_is_held(monkeypatch):
    _replied(monkeypatch, True)
    assert "written to them yourself" in asyncio.run(loop.stale_draft(_draft()))


def test_an_old_draft_is_held_without_asking_teams(monkeypatch):
    seen = _replied(monkeypatch, False)
    why = asyncio.run(loop.stale_draft(_draft(age=loop.DRAFT_FRESH_SECONDS + 600)))
    assert "drafted" in why and "minutes ago" in why and not seen


def test_a_failed_look_does_not_hold_a_fresh_draft(monkeypatch):
    async def broken(chat, at):
        raise RuntimeError("browser gone")
    monkeypatch.setattr(chat_watch, "he_replied_since", broken)
    assert asyncio.run(loop.stale_draft(_draft())) == ""


def test_only_teams_drafts_to_a_person_are_checked(monkeypatch):
    seen = _replied(monkeypatch, True)
    assert asyncio.run(loop.stale_draft(_draft(channel="pr"))) == ""
    assert asyncio.run(loop.stale_draft({**_draft(), "_at": 0})) == ""
    assert not seen
