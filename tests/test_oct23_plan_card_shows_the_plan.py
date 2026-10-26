"""23 Oct, #406: the plan card on his phone read "Plan's posted and waiting on your
approval" and nothing else. The brain had put the plan in report_outcome and ended
on a sign-off; the card printed the sign-off."""
import asyncio
import json
import time

from app import go, notify, store, tasks
from app.graph import outcome

PLAN = ("A null entry in BookingEquipment.commodities bypasses validation because "
        "@NotEmpty only checks list size. Plan: List<@NotNull CommodityCodes> commodities, "
        "a COMMODITY_CODE_NULL message, and a regression test for a [null] list.")


def _announce(monkeypatch, tid, result):
    sent = []

    async def _notify(text, *a, **k):
        sent.append(text)
        return {}

    monkeypatch.setattr(notify, "notify", _notify)
    monkeypatch.setattr(go, "no_ask", lambda *a, **k: "")
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), result))
    return sent[-1]


def test_a_sign_off_card_carries_the_reported_plan(monkeypatch):
    tid = store.create_task(title="Fix booking PR 1489", kind="code", prompt="x", workspace="booking")["id"]
    store.kv_set(f"task_outcome:{tid}", json.dumps(
        {"kind": "plan_ready", "summary": PLAN, "at": time.time()}))
    card = _announce(monkeypatch, tid,
                     "Plan's posted and waiting on your approval — nothing further to do until you say go.")
    assert "List<@NotNull CommodityCodes>" in card
    assert f"approve task {tid}" in card


def test_a_plan_is_not_cut_at_600_characters():
    tid = store.create_task(title="t", kind="code", prompt="x", workspace="booking")["id"]
    long = "step " * 250
    outcome.record(tid, "plan_ready", long)
    assert len(json.loads(store.kv_get(f"task_outcome:{tid}"))["summary"]) > 600


def test_a_long_cause_does_not_push_the_fix_off_the_card():
    """26 Oct, #419: "## 1. CAUSE" filled the card; "## 2. PLAN" was cut."""
    plan = ("## 1. CAUSE\n\n" + "\n".join(f"Cause detail line {i} " + "x" * 80 for i in range(30))
            + "\n\n## 2. PLAN\n\nMove the LaunchDarkly check inside the existing activities.")
    card = tasks._phone_text(plan, 1800, head_first=True)
    assert "## 2. PLAN" in card and "inside the existing activities" in card
    assert "Cause detail line 0" in card


def test_a_plan_without_a_cause_section_reads_as_before():
    plan = "## Plan\n\n1. Add the field.\n2. Map it."
    assert tasks._phone_text(plan, 1800, head_first=True) == plan
