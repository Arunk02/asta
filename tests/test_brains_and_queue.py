"""Every ask worked, every brain measured, every limit said exactly.

His words, 30 Sep: "5 people pinging at a time for different issues — you do
only 2 now and the remaining 3 after hours? No, this is not right. If the
session is going to be reached, tell me upfront; do as much as you can and
continue from there when it comes back. Copilot monthly quota out — tell it
exactly — and ask if I want to switch brains to pick up from there."
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import store


# --- the queue: none dropped, a few at once, most urgent first ---------------------------

def test_five_asks_at_once_run_three_and_queue_two_in_priority_order(monkeypatch):
    from app import tasks
    monkeypatch.setenv("ASTA_MAX_PARALLEL_INVESTIGATIONS", "3")
    order, gate = [], tasks._Gate()
    monkeypatch.setattr(tasks, "_gate", gate)
    ids = []
    for i, prio in enumerate([2, 2, 2, 2, 0]):          # the last one he asked for himself
        t = store.create_task(f"ask {i}", "analysis", "p", None)
        store.kv_set(f"task_priority:{t['id']}", str(prio))
        ids.append(t["id"])

    async def one(tid, hold):
        async with tasks.investigation_slot(tid):
            order.append(tid)
            await hold.wait()

    async def run():
        holds = {tid: asyncio.Event() for tid in ids}
        jobs = [asyncio.create_task(one(tid, holds[tid])) for tid in ids]
        await asyncio.sleep(0.01)
        running_first = list(order)
        queued = [store.get_task(t)["status"] for t in ids[3:]]
        holds[ids[0]].set()
        await asyncio.sleep(0.01)
        for h in holds.values():
            h.set()
        await asyncio.gather(*jobs)
        return running_first, queued

    running_first, queued = asyncio.run(run())
    assert running_first == ids[:3]
    assert queued == ["queued", "queued"], "waiting, visibly — not dropped"
    assert order[3] == ids[4], "the one he asked for himself goes next"
    assert sorted(order) == sorted(ids), "every one of them ran"


def test_a_queued_investigation_counts_as_live():
    from app import tasks
    assert "queued" in tasks.LIVE_STATUSES


# --- the Claude budget, measured from the usage logs ---------------------------------------

def _log(dir_, name, rows):
    d = dir_ / name
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "s.jsonl", "w") as fh:
        for i, (ts, model, u) in enumerate(rows):
            for _ in range(2):          # the CLI repeats a message per content block
                fh.write(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                                                  time.gmtime(ts)),
                                     "message": {"id": f"{name}-{i}", "model": model,
                                                 "usage": u}}) + "\n")


@pytest.fixture
def logs(tmp_path, monkeypatch):
    from app import brains
    monkeypatch.setattr(brains, "PROJECTS", tmp_path)
    monkeypatch.setattr(brains, "_cache", {})
    return tmp_path


def test_usage_is_weighted_and_counted_once(logs):
    from app import brains
    now = time.time()
    _log(logs, "-Users-arun-k-k-help", [(now - 60, "claude-opus-4-8",
                                        {"output_tokens": 1000, "cache_read_input_tokens": 10**6})])
    _log(logs, "-Users-arun-k-k-help-asta", [(now - 30, "claude-sonnet-5",
                                             {"cache_creation_input_tokens": 800})])
    s = brains.claude_status(now)
    assert s["yours"] == pytest.approx(5 * 5000)            # opus ×5, output ×5, cache reads 0
    assert s["asta"] == pytest.approx(1000)                 # cache write ×1.25
    assert s["used"] == pytest.approx(26000)


def test_the_ceiling_is_learned_from_a_real_limit_hit(logs):
    from app import brains
    now = time.time()
    _log(logs, "x", [(now - 3000, "claude-sonnet-5", {"output_tokens": 2_000_000})])
    brains.record_hit(now, now + 600)
    assert brains.ceiling() == pytest.approx(10_000_000)
    assert json.loads(store.kv_get("claude_resets"))[-1] == pytest.approx(now + 600)


def test_he_is_told_once_at_80_percent_with_what_is_running_and_the_way_out(logs, monkeypatch):
    from app import brains
    now = time.time()
    store.kv_set("claude_limit_levels", json.dumps([1_000_000]))
    _log(logs, "-Users-arun-k-k-help", [(now - 60, "claude-sonnet-5", {"output_tokens": 170_000})])
    sent = []

    async def notify(text, *a, **k):
        sent.append(text)

    s = asyncio.run(brains.tick(notify, now))
    assert s["share"] == pytest.approx(0.85)
    assert len(sent) == 1 and "85%" in sent[0] and "resets" in sent[0]
    asyncio.run(brains.tick(notify, now + 60))
    asyncio.run(brains.tick(notify, now + 1800))
    assert len(sent) == 1, "once per window"
    assert brains.tight()


def test_the_reader_steps_down_when_claude_is_tight(monkeypatch):
    from app import understand
    monkeypatch.setenv("ASTA_UNDERSTAND_MODEL", "sonnet")
    monkeypatch.setenv("ASTA_UNDERSTAND_FALLBACK_MODEL", "haiku")
    store.kv_set("brains_status", json.dumps({"share": 0.75, "limited": False}))
    assert understand.model() == "haiku"
    store.kv_set("brains_status", json.dumps({"share": 0.3, "limited": False}))
    assert understand.model() == "sonnet"


def test_copilot_says_the_date_it_renews(monkeypatch):
    from app import agent, brains
    monkeypatch.setattr(agent, "quota_down", lambda name: True)
    import datetime as dt
    now = dt.datetime(2026, 9, 30, 12, 0).timestamp()
    assert brains.copilot_status(now) == {"out": True, "resets_on": "1 Oct"}


def test_brain_status_is_every_brain_exactly(logs, monkeypatch):
    from app import agent, brains
    monkeypatch.setattr(agent, "quota_down", lambda name: name == "copilot")
    monkeypatch.setattr(brains, "local_status", lambda: {"on": False})
    text = brains.brain_status()
    assert text.startswith("Claude: ")
    assert "Copilot: monthly quota out, resets" in text
    assert "Local model: off" in text and "Work: " in text


# --- a limit: one message, exact, with the real way out ------------------------------------

def test_many_pauses_are_one_message(monkeypatch):
    from app import agent, notify, tasks
    sent = []

    async def _notify(text, *a, **k):
        sent.append(text)

    monkeypatch.setattr(notify, "notify", _notify)
    monkeypatch.setattr(agent, "quota_down", lambda name: name == "copilot")
    monkeypatch.setenv("ASTA_LIMIT_NOTICE_GATHER_SECONDS", "0.05")
    reset = time.time() + 3600
    rows = [store.create_task(title, "analysis", "p", None)
            for title in ("Vinish booking", "Harika filters")]

    async def two_pauses_seconds_apart():
        store.update_task(rows[0]["id"], status="paused")
        first = asyncio.create_task(tasks._tell_the_limit_once("claude", reset))
        await asyncio.sleep(0.01)
        store.update_task(rows[1]["id"], status="paused")
        await tasks._tell_the_limit_once("claude", reset)
        await first

    asyncio.run(two_pauses_seconds_apart())
    assert len(sent) == 1
    msg = sent[0]
    assert msg.startswith("⏸ Claude hit its session limit — it resets")
    assert "Vinish booking" in msg and "Harika filters" in msg
    assert "Copilot's monthly quota is out until" in msg, "the switch is only offered when it is real"


def test_switching_brains_moves_the_paused_work(monkeypatch):
    from app import agent, claude_cli, tasks
    monkeypatch.setattr(agent, "available", lambda name: True)
    monkeypatch.setattr(agent, "quota_down", lambda name: False)
    monkeypatch.setattr(claude_cli, "limited_until", lambda now=None: 0.0)
    moved = []

    async def resume(tid, switch_to=""):
        moved.append((tid, switch_to))
        return "ok"

    monkeypatch.setattr(tasks, "resume_task", resume)
    t = store.create_task("Vinish booking", "analysis", "p", None)
    store.update_task(t["id"], status="paused")
    out = asyncio.run(tasks.use_brain("copilot"))
    assert moved == [(t["id"], "copilot")] and f"#{t['id']}" in out
    assert tasks.brain_override() == "copilot"


def test_switching_to_a_brain_that_is_out_says_so(monkeypatch):
    from app import agent, tasks
    monkeypatch.setattr(agent, "available", lambda name: True)
    monkeypatch.setattr(agent, "quota_down", lambda name: name == "copilot")
    out = asyncio.run(tasks.use_brain("copilot"))
    assert "monthly quota is out until" in out and tasks.brain_override() == ""


@pytest.mark.parametrize("text", ["brain status", "quota", "how much claude is left", "usage?"])
def test_brain_status_is_asked_in_plain_words(text):
    from app import main
    assert main._BRAIN_STATUS.match(text)


def test_windows_are_fixed_blocks_not_a_rolling_five_hours(logs):
    from app import brains
    import datetime as dt
    t0 = dt.datetime(2026, 9, 30, 9, 20).timestamp()
    evs = [(t0, 1.0, False), (t0 + 3600, 1.0, False), (t0 + 6 * 3600, 1.0, False)]
    assert brains.window_start(t0 + 2 * 3600, evs) == t0
    assert brains.window_start(t0 + 6 * 3600 + 60, evs) == t0 + 6 * 3600, "a new block after five hours"
    store.kv_set("claude_resets", json.dumps([t0 + 3 * 3600]))
    assert brains.window_start(t0 + 2 * 3600, evs) == t0 - 2 * 3600, "a stated reset wins"
