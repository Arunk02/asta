"""Every run's cost is recorded beside its tokens — chat, coding, debugging and
background alike — from Claude Code's own figure (18 Nov)."""
from __future__ import annotations

import asyncio
import json
import time

from app import claude_cli, store, usage_week, worker_usage


def _json(text, total, sid="s1"):
    return json.dumps({"type": "result", "result": text, "total_cost_usd": total,
                       "session_id": sid, "usage": {"output_tokens": 5}})


def test_a_run_is_charged_its_difference_from_the_sessions_last_total():
    assert claude_cli._parse_run(_json("first", 0.010, "sx")) == "first"
    assert round(claude_cli.LAST_COST.get(), 6) == 0.010
    assert claude_cli._parse_run(_json("second", 0.016, "sx")) == "second"
    assert round(claude_cli.LAST_COST.get(), 6) == 0.006          # a resume: the running total
    assert claude_cli._parse_run(_json("new session", 0.002, "sy")) == "new session"
    assert round(claude_cli.LAST_COST.get(), 6) == 0.002


def test_anything_that_is_not_the_result_object_reads_as_before():
    assert claude_cli._parse_run("Not logged in · Please run /login") == \
        "Not logged in · Please run /login"
    assert claude_cli.LAST_COST.get() is None


class _Proc:
    def __init__(self, data):
        self._d = data.encode(); self.returncode = 0; self.stdout = self

    async def read(self, n=-1):
        out, self._d = self._d[:512], self._d[512:]
        return out

    async def wait(self):
        return 0

    def kill(self):
        pass


def test_the_run_text_is_unchanged_for_its_callers(monkeypatch):
    async def spawn(*a, **k):
        return _Proc(_json("[x] Stage 2 done. The answer.", 0.004, "bg1"))
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", spawn)
    seen = []

    async def progress(text):
        seen.append(text)
    out = asyncio.run(claude_cli._one_shot("x", None, 60, "", "", "", False, progress,
                                           "", False, "haiku", False))
    assert out == "[x] Stage 2 done. The answer."
    assert seen == [out], "the milestone watcher reads the text, as before"


def test_a_background_call_records_its_cost():
    from app import call_ledger
    with call_ledger.call("claude", "/tmp", "bg2", purpose="unit-test"):
        claude_cli._parse_run(_json("ok", 0.004, "bg2"))
    row = store.call_usage_since(time.time() - 60)[-1]
    assert row["purpose"] == "unit-test" and round(row["cost_usd"], 6) == 0.004


def test_a_task_leg_records_its_cost():
    t = store.create_task("fix", "code", "p", "booking")
    with worker_usage.attempt(t["id"], "claude", "/tmp", "s9", stage="build"):
        claude_cli._parse_run(_json("done", 0.03, "s9"))
    row = json.loads(next(iter(store.kv_like(f"worker_usage:{t['id']}:%").values())))
    assert round(row["cost_usd"], 6) == 0.03


def test_a_crashed_leg_never_inherits_the_last_figure():
    claude_cli._parse_run(_json("earlier", 0.5, "s10"))
    t = store.create_task("fix", "code", "p", "booking")
    try:
        with worker_usage.attempt(t["id"], "claude", "/tmp", "s11", stage="build"):
            raise RuntimeError("timed out")
    except RuntimeError:
        pass
    row = json.loads(next(iter(store.kv_like(f"worker_usage:{t['id']}:%").values())))
    assert row["cost_usd"] is None


def test_the_week_shows_dollars_for_every_kind_of_work():
    code = store.create_task("fix", "code", "p", "booking")["id"]
    dbg = store.create_task("check booking", "analysis", "p", "booking")["id"]
    for tid, usd in ((code, 0.40), (dbg, 0.10)):
        store.kv_set(f"worker_usage:{tid}:r", json.dumps({
            "task_id": tid, "run_id": "r", "started_at": time.time() - 60, "cost_usd": usd,
            "usage": {"new_tokens": 1000, "cache_read": 0, "out_tokens": 10, "calls": 1}}))
    out = usage_week.table()
    assert "coding" in out and "$0.40" in out and "debugging" in out and "$0.10" in out
    assert "All of it: $0.50 at API prices" in out
