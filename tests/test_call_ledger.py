"""Background one-shot calls are measured from their own session log, or marked unavailable."""
import asyncio
import json

import pytest

from app import agent, call_ledger, claude_cli, copilot_cli, store, token_audit, worker_usage

_REAL = {claude_cli: claude_cli.one_shot, copilot_cli: copilot_cli.one_shot}


def _write_log(path, mid, output):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "assistant", "message": {"id": mid, "usage": {
            "input_tokens": 3, "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 50, "output_tokens": output}, "content": []}}) + "\n")


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A stand-in claude/copilot process that writes its own session log like the real CLI."""
    monkeypatch.setattr(token_audit, "CLAUDE_PROJECTS", tmp_path / "logs")
    seen = {"argv": [], "exit": 0, "project": None}

    class _Proc:
        returncode = 0

        class stdout:
            done = False

            @staticmethod
            async def read(_n):
                if _Proc.stdout.done:
                    return b""
                _Proc.stdout.done = True
                return b"an answer"

        @staticmethod
        async def wait():
            _Proc.returncode = seen["exit"]
            return seen["exit"]

    async def fake(*cmd, **kw):
        cmd = list(cmd)
        seen["argv"].append(cmd)
        if cmd[0] == "claude" and "--session-id" in cmd:
            sid = cmd[cmd.index("--session-id") + 1]
            project = seen["project"] or token_audit._encode_project(kw["cwd"])
            _write_log(token_audit.CLAUDE_PROJECTS / project / f"{sid}.jsonl", "m1", 11)
        _Proc.stdout.done = False
        return _Proc()

    for mod in (claude_cli, copilot_cli):
        monkeypatch.setattr(mod, "available", lambda: True)
        monkeypatch.setattr(mod, "one_shot", _REAL[mod])
    monkeypatch.setattr(claude_cli, "limited_until", lambda: 0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return seen


def _rows():
    return store.call_usage_since(0)


def test_a_background_claude_call_is_measured_from_its_own_session(cli, tmp_path):
    out = asyncio.run(claude_cli.one_shot("summarise the day", cwd=str(tmp_path),
                                          model="haiku", purpose="memory.consolidate"))
    assert out == "an answer"
    assert "--session-id" in cli["argv"][0], "an unnamed run cannot be found afterwards"
    [row] = _rows()
    assert (row["purpose"], row["executor"], row["model"], row["status"]) == \
        ("memory.consolidate", "claude", "haiku", "returned")
    assert row["usage_state"] == "provider_reported"
    assert (row["calls"], row["new_tokens"], row["cache_read"], row["out_tokens"]) == (1, 10, 50, 11)


def test_a_log_under_a_resolved_project_name_is_still_found(cli, tmp_path):
    """The CLI may name the project after the resolved cwd (/tmp → /private/tmp)."""
    cli["project"] = "-private-elsewhere"
    asyncio.run(claude_cli.one_shot("x", cwd=str(tmp_path), purpose="refresh.enrich"))
    assert _rows()[0]["usage_state"] == "provider_reported"


def test_a_task_worker_leg_is_not_recorded_twice(cli, tmp_path):
    task = store.create_task("pilot", "code", "fix the flaky test", None)
    with worker_usage.attempt(task["id"], "claude", str(tmp_path), "leg-1", stage="implement"):
        asyncio.run(claude_cli.one_shot("implement", cwd=str(tmp_path), session_id="leg-1"))
    assert _rows() == [], "worker_usage already owns this leg"
    assert worker_usage.report(task["id"])["total_attempts"] == 1
    asyncio.run(claude_cli.one_shot("after the task", cwd=str(tmp_path)))
    assert len(_rows()) == 1, "the flag must not outlive the attempt"


def test_a_copilot_call_is_counted_but_never_as_zero(cli, tmp_path):
    asyncio.run(copilot_cli.one_shot("triage this", cwd=str(tmp_path), purpose="reader.classify"))
    [row] = _rows()
    assert row["executor"] == "copilot" and row["usage_state"] == "unavailable"
    assert row["new_tokens"] is None and row["out_tokens"] is None
    text = call_ledger.summary(24)
    assert "0/1 measured" in text and "usage unavailable, not zero" in text


def test_a_failed_call_keeps_its_row_and_a_refused_one_makes_none(cli, tmp_path, monkeypatch):
    cli["exit"] = 1
    with pytest.raises(RuntimeError):
        asyncio.run(claude_cli.one_shot("x", cwd=str(tmp_path), purpose="review.second_look"))
    [row] = _rows()
    assert row["status"] == "failed" and row["error_type"] == "RuntimeError"
    assert row["usage_state"] == "provider_reported", "a failed run still spent what it spent"
    monkeypatch.setattr(claude_cli, "limited_until", lambda: 4_102_444_800)
    with pytest.raises(RuntimeError, match="usage limit"):
        asyncio.run(claude_cli.one_shot("x", cwd=str(tmp_path)))
    assert len(_rows()) == 1, "a call that was never made is not a call"


def test_the_purpose_defaults_to_the_app_function_that_asked():
    scope = {"__name__": "app.fake_brain", "call_ledger": call_ledger}
    exec("def digest():\n    return call_ledger.caller()\n", scope)
    assert scope["digest"]() == "fake_brain.digest"
    assert call_ledger.caller() == "unattributed", "a test frame is not an app purpose"


def test_the_report_groups_by_purpose_and_reaches_the_trace_tool(cli, tmp_path):
    for purpose in ("memory.consolidate", "memory.consolidate", "understand.label"):
        asyncio.run(claude_cli.one_shot("x", cwd=str(tmp_path), purpose=purpose))
    data = call_ledger.report(24)
    assert data["calls"] == 3 and data["measured_calls"] == 3 and data["monetary_cost"] is None
    assert list(data["purposes"]) == ["memory.consolidate", "understand.label"]
    assert data["purposes"]["memory.consolidate"]["usage"]["out_tokens"] == 22
    assert "memory.consolidate: 2 calls (2 measured)" in agent.trace_report(1)


def test_the_table_stays_bounded(monkeypatch):
    monkeypatch.setattr(store, "CALL_USAGE_KEEP", 3)
    for n in range(5):
        store.add_call_usage(f"r{n}", purpose="p", executor="claude", model="m")
    assert [row["run_id"] for row in _rows()] == ["r2", "r3", "r4"]


def test_an_awaited_call_is_attributed_to_its_awaiting_app_function(cli, tmp_path):
    scope = {"__name__": "app.fake_brain", "claude_cli": claude_cli, "cwd": str(tmp_path)}
    exec("async def nightly_digest():\n    return await claude_cli.one_shot('x', cwd=cwd)\n", scope)
    asyncio.run(scope["nightly_digest"]())
    assert _rows()[0]["purpose"] == "fake_brain.nightly_digest"
