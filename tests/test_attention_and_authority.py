"""Interrupting less, and doing more alone — Astra-class P5.

The WorkWorld set `attention.yaml` and the simulated day prove the behaviour;
these pin the parts: what the budget counts, when a source stops interrupting,
what a permission is allowed to do, and what the morning line says.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import attention, authority, briefing, budget, digest, frontdesk, ops, policy, store


# --- the day's budget of interruptions -------------------------------------------------

def test_the_budget_counts_buzzes_and_spares_breakage(monkeypatch):
    monkeypatch.setenv("ASTA_PUSH_BUDGET", "3")
    for _ in range(3):
        budget.note_push()
    assert budget.spent() == 3 and budget.left() == 0
    assert not budget.allows(attention.P_TODAY)          # ordinary news waits
    assert not budget.allows(attention.P_FYI)
    assert budget.allows(attention.P_NOW)                # breakage never waits
    monkeypatch.setenv("ASTA_PUSH_BUDGET", "0")
    assert budget.allows(attention.P_FYI)                # 0 = no budget at all


def test_yesterdays_budget_is_not_todays(monkeypatch):
    monkeypatch.setenv("ASTA_PUSH_BUDGET", "1")
    budget.note_push(now=time.time() - 24 * 3600)
    assert budget.spent() == 0 and budget.allows(attention.P_FYI)


def test_past_the_budget_it_goes_to_the_digest(monkeypatch):
    from app import notify
    monkeypatch.setenv("ASTA_PUSH_BUDGET", "1")
    sent: list[str] = []

    async def wa(text):
        sent.append(text)
        return True

    async def tg(text, *a, **k):
        return True

    monkeypatch.setattr(notify, "wa_send", wa)
    monkeypatch.setattr(notify.telegram, "send", tg)
    asyncio.run(notify.notify("first thing", "teams", priority=attention.P_TODAY))
    asyncio.run(notify.notify("second thing", "teams", priority=attention.P_TODAY))
    assert sent == ["first thing"]
    assert [r["text"] for r in digest.pending()] == ["second thing"]


# --- learned from his own reactions ------------------------------------------------------

def _seed(source: str, who: str, seen: int, ignored: int) -> None:
    now = time.time()
    for i in range(seen):
        at = now - 5 * 86400
        key = f"k{i}"
        store.attention_upsert(key, source, who=who, what=f"item {i}",
                               priority=attention.P_TODAY, now=at)
        if i < ignored:
            store.attention_set(key, state="notified", notified_at=at)
        else:
            store.attention_set(key, state="acted", notified_at=at, acted_at=at + 60)


def test_a_source_he_ignores_moves_to_the_digest_and_one_he_answers_does_not():
    _seed("outlook", "IT Service Desk", 20, 18)
    assert attention.history("outlook", "IT Service Desk") == (2, 18)
    assert "ignored 18 of the last 20" in attention.to_digest("outlook", "IT Service Desk")
    _seed("teams-chat", "Dana Frost", 20, 2)
    assert attention.to_digest("teams-chat", "Dana Frost") == ""


def test_a_thin_record_never_demotes_and_breakage_never_does():
    _seed("outlook", "New Sender", 6, 6)
    assert attention.to_digest("outlook", "New Sender") == ""          # too few to judge
    _seed("outlook", "Loud Sender", 20, 20)
    assert attention.to_digest("outlook", "Loud Sender", attention.P_NOW) == ""


def test_his_word_beats_his_record_in_both_directions():
    _seed("outlook", "IT Service Desk", 20, 20)
    attention.set_force("IT Service Desk", "push")
    assert attention.to_digest("outlook", "IT Service Desk") == ""
    attention.set_force("IT Service Desk", "digest")
    assert "asked for this one" in attention.to_digest("outlook", "IT Service Desk")


def test_the_demotion_notice_is_said_once():
    first = attention.note_demoted("outlook", "IT Service Desk", "you ignored 18 of 20")
    assert "push IT Service Desk" in first
    assert attention.note_demoted("outlook", "IT Service Desk", "you ignored 18 of 20") == ""


def test_learning_can_be_switched_off(monkeypatch):
    _seed("outlook", "IT Service Desk", 20, 20)
    monkeypatch.setenv("ASTA_ATTENTION_LEARN", "0")
    assert attention.to_digest("outlook", "IT Service Desk") == ""


# --- the digest ----------------------------------------------------------------------------

def test_the_digest_holds_groups_and_empties():
    digest.add("Change CHG1 scheduled", source="IT Service Desk")
    digest.add("Change CHG2 scheduled", source="IT Service Desk")
    digest.add("a build finished", source="ci")
    body = digest.render(digest.pending(), reason="midday digest")
    assert "3 things" in body and "*IT Service Desk* (2)" in body and "*ci* (1)" in body
    assert len(digest.take()) == 3 and digest.pending() == []


def test_the_digest_goes_out_once_per_slot(monkeypatch):
    from app import notify
    sent: list[str] = []

    async def deliver(text):
        sent.append(text)
        return {"whatsapp": True}

    monkeypatch.setattr(notify, "deliver", deliver)
    midday = time.mktime(time.strptime("2026-09-16 13:30", "%Y-%m-%d %H:%M"))
    digest.add("something quiet", source="ci")
    assert asyncio.run(digest.tick(now=midday))["items"] == 1
    digest.add("another quiet thing", source="ci")
    assert asyncio.run(digest.tick(now=midday))["sent"] is False   # already went at midday
    morning = time.mktime(time.strptime("2026-09-16 08:00", "%Y-%m-%d %H:%M"))
    assert digest.due(now=morning) == ""                            # the brief carries it
    assert len(sent) == 1


# --- what Asta may do alone ------------------------------------------------------------------

def test_a_permission_is_capped_per_day_and_revocable():
    g = authority.grant("send", "Dana Frost", per_day=1)
    assert authority.may("send", "dana frost") is not None
    authority.note_use(g)
    assert authority.may("send", "Dana Frost") is None              # spent for today
    assert authority.used_today(g.id) == 1
    assert authority.revoke(g.id) and authority.grants() == []


def test_a_standing_rule_outranks_a_permission():
    authority.grant("send", "Release Channel", per_day=5)
    policy.add("never", "send", "Release Channel", words="never message the release channel")
    assert authority.may("send", "Release Channel") is None


def test_a_permission_is_only_earned_after_ten_of_the_same():
    for i in range(9):
        authority.note_approved("send", "Dana Frost")
    assert not authority.earned("send", "Dana Frost")
    authority.note_approved("send", "Dana Frost")
    assert authority.earned("send", "Dana Frost")
    assert not authority.earned("send", "Someone Else")             # never a new person
    authority.propose("send", "Dana Frost")
    assert not authority.earned("send", "Dana Frost")               # never asked twice


def test_his_yes_to_a_permission_grants_it():
    out = asyncio.run(ops.run({"name": "authority_grant",
                               "args": {"act": "send", "target": "Dana Frost", "per_day": 2}}))
    assert "Permission 1" in out and authority.grants()[0].per_day == 2


def test_permissions_are_listed_and_revoked_from_the_front_desk():
    g = authority.grant("send", "Dana Frost", per_day=1)
    assert "May message Dana Frost" in frontdesk.answer_from_state("my permissions")
    assert "revoked" in frontdesk.answer_from_state(f"revoke {g.id}")
    assert "no standing permissions" in frontdesk.answer_from_state("my permissions")


def test_the_digest_can_be_read_and_a_source_steered_without_a_brain():
    digest.add("Change CHG1 scheduled", source="IT Service Desk")
    assert "CHG1" in frontdesk.answer_from_state("what's in the digest")
    attention.note_demoted("outlook", "IT Service Desk", "ignored a lot")
    assert "interrupt you again" in frontdesk.answer_from_state("push IT Service Desk")
    # And an ordinary sentence that happens to start with "push" is left alone.
    assert frontdesk.answer_from_state("push the branch") == ""


# --- one morning line --------------------------------------------------------------------------

def test_the_morning_line_says_what_yesterday_cost_him():
    cutoff = time.time() - 24 * 3600
    done = store.create_task("fix the mapper", "code", "p", None)
    store.update_task(done["id"], status="done", finished_at=time.time())
    waiting = store.create_task("plan for the ACL change", "code", "p", None)
    store.update_task(waiting["id"], status="awaiting_approval")
    g = authority.grant("send", "Dana Frost")
    authority.note_use(g, "a nudge")
    digest.add("something quiet", source="ci")
    line = briefing.yesterday_line(cutoff)
    assert line.startswith("Yesterday: 1 handled, 1 need you")
    assert "1 done under your standing permissions" in line and "1 read, not buzzed" in line


# --- the bar P5 is judged by -------------------------------------------------------------------

def test_a_day_in_his_life_stays_within_the_budget_and_misses_nothing():
    """P5's exit criterion, enforced rather than admired: ≤ 20 interruptions for
    150 events, and nothing he needed goes unsaid. Measured through the real
    notify path — the ledger, the digest, the budget and the batching."""
    from app.workworld import day
    res = asyncio.run(day.run())
    assert len(res.pushes) <= 20, day.report(res)
    assert res.missed == [], day.report(res)
    assert res.false_interrupts == [], day.report(res)
