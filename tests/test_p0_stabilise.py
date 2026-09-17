"""P0 of the Astra-class plan: the failures the 11 September audit measured.

Each block below is one line of that audit, with the number that justified it:
  · 8 analysis tasks failed on a Claude session limit instead of pausing
  · 15 jira_issue calls died as tracebacks on a comments 404
  · 43 ASGI tracebacks, many from /api/_invoke passing a tool's exception raw
  · the Atlassian MCP login opened a browser tab from inside the server
  · a log with no timestamps
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from app import jira, store, tasks

LIMIT = "claude exited 1: You've hit your session limit · resets 5:10pm (Asia/Calcutta)"
COPILOT_OUT = "You have exceeded your monthly quota for premium requests"


# --- usage limits pause on every path ---------------------------------------

@pytest.fixture
def brains(monkeypatch, tmp_path):
    """Scripted claude/copilot one-shots and a notify recorder."""
    calls: list[str] = []
    script: dict[str, list] = {"claude": [], "copilot": []}
    pushed: list[str] = []

    def make(name):
        async def one_shot(prompt, **kw):
            calls.append(name)
            step = script[name].pop(0) if script[name] else "done"
            if isinstance(step, Exception):
                raise step
            return step
        return one_shot

    monkeypatch.setattr(tasks.claude_cli, "one_shot", make("claude"))
    monkeypatch.setattr(tasks.copilot_cli, "one_shot", make("copilot"))
    monkeypatch.setattr(tasks.claude_cli, "available", lambda: True)
    monkeypatch.setattr(tasks.copilot_cli, "available", lambda: True)
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(tmp_path))
    monkeypatch.setattr(tasks, "_audit_note", lambda tid: "")
    monkeypatch.setattr(tasks, "_learn_from", lambda *a, **k: None)

    async def notify(msg, kind="task", **k):
        pushed.append(msg)
    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    return {"calls": calls, "script": script, "pushed": pushed}


def _analysis(executor: str) -> int:
    t = store.create_task("why is the booking stuck", "analysis", "look at the logs", None)
    store.kv_set(f"task_executor:{t['id']}", executor)
    return t["id"]


def test_an_analysis_that_hits_a_limit_pauses_instead_of_failing(brains, monkeypatch):
    """The 9–10 Sep failure: claude limited, copilot out for the month."""
    from app import agent
    agent.mark_quota_down("copilot", COPILOT_OUT)
    brains["script"]["claude"] = [RuntimeError(LIMIT)]
    tid = _analysis("claude")
    asyncio.run(tasks._worker(tid))
    t = store.get_task(tid)
    assert t["status"] == "paused", t
    assert store.kv_get(f"task_resume_at:{tid}"), "an auto-resume must be scheduled"
    assert any("paused" in p and "run it again" in p for p in brains["pushed"])


def test_a_limited_brain_hands_the_run_to_one_that_is_up(brains):
    brains["script"]["claude"] = [RuntimeError(LIMIT)]
    brains["script"]["copilot"] = ["the consumer lag explains it"]
    tid = _analysis("claude")
    asyncio.run(tasks._worker(tid))
    assert brains["calls"] == ["claude", "copilot"]
    assert store.get_task(tid)["status"] == "done"
    assert store.kv_get(f"task_executor:{tid}") == "copilot"


def test_both_limited_pauses_on_the_one_that_comes_back_first(brains):
    brains["script"]["copilot"] = [RuntimeError(COPILOT_OUT)]
    brains["script"]["claude"] = [RuntimeError(LIMIT)]
    tid = _analysis("copilot")
    asyncio.run(tasks._worker(tid))
    assert store.get_task(tid)["status"] == "paused"
    # Copilot's monthly pool names no time; Claude's window does.
    assert store.kv_get(f"task_executor:{tid}") == "claude"


def test_a_crash_is_still_a_failure(brains):
    """Only a LIMIT pauses. A real error must not be dressed up as a wait."""
    brains["script"]["claude"] = [RuntimeError("claude exited 1: invalid flag --foo")]
    tid = _analysis("claude")
    asyncio.run(tasks._worker(tid))
    assert store.get_task(tid)["status"] == "failed"


def test_a_paused_analysis_resumes_by_running_again(brains, monkeypatch):
    from app import agent
    agent.mark_quota_down("copilot", COPILOT_OUT)
    brains["script"]["claude"] = [RuntimeError(LIMIT), "found it"]
    tid = _analysis("claude")
    asyncio.run(tasks._worker(tid))
    assert store.get_task(tid)["status"] == "paused"
    store.kv_del("claude_cli_quota_until")
    store.kv_del(agent.quota_kv("claude_cli"))

    async def resume_and_wait():
        msg = await tasks.resume_task(tid)
        await tasks._running[tid]
        return msg

    msg = asyncio.run(resume_and_wait())
    assert "running it again" in msg
    assert store.get_task(tid)["status"] == "done"


def test_a_draft_pauses_too(brains):
    from app import agent
    agent.mark_quota_down("copilot", COPILOT_OUT)
    brains["script"]["claude"] = [RuntimeError(LIMIT)]
    t = store.create_task("reply", "teams_draft", "draft it", None, "Someone")
    store.kv_set(f"task_executor:{t['id']}", "claude")
    asyncio.run(tasks._worker(t["id"]))
    assert store.get_task(t["id"])["status"] == "paused"


# --- a restart does not strand work ------------------------------------------

def test_a_task_left_running_by_a_restart_is_picked_back_up(brains):
    """#117's shape: the row says running, no worker exists, and every message
    Arun sends is routed into it for ever."""
    t = store.create_task("implement the mapping", "code", "do it", None)
    assert store.get_task(t["id"])["status"] == "running"
    picked = asyncio.run(tasks.recover_orphans())
    assert picked == [t["id"]]
    row = store.get_task(t["id"])
    assert row["status"] == "paused" and "restart" in row["error"]
    assert float(store.kv_get(f"task_resume_at:{t['id']}")) > 0
    assert any("restarted" in p for p in brains["pushed"])


def test_a_task_this_process_is_actually_running_is_left_alone(brains, monkeypatch):
    t = store.create_task("live one", "analysis", "p", None)
    monkeypatch.setattr(tasks, "is_running", lambda tid: tid == t["id"])
    assert asyncio.run(tasks.recover_orphans()) == []
    assert store.get_task(t["id"])["status"] == "running"


def test_a_task_waiting_on_him_is_not_disturbed(brains):
    t = store.create_task("plan me", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval", result="PLAN READY")
    assert asyncio.run(tasks.recover_orphans()) == []
    assert store.get_task(t["id"])["status"] == "awaiting_approval"


def test_the_recovery_runs_at_startup():
    from pathlib import Path
    assert 'daemon.once("recover-orphans", tasks.recover_orphans())' in Path("app/main.py").read_text()


# --- jira: a comments 404 no longer sinks the issue --------------------------

def _jira(monkeypatch, handler):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.c")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(jira, "_client", lambda: httpx.AsyncClient(
        base_url="https://example.atlassian.net", transport=transport))


_ISSUE = {"key": "ABC-1", "fields": {"summary": "Fix it", "status": {"name": "Open"},
                                     "description": None, "labels": [], "components": []}}


def test_an_issue_whose_comments_404_still_comes_back(monkeypatch):
    def handler(req):
        if req.url.path.endswith("/comment"):
            return httpx.Response(404, json={"errorMessages": ["nope"]})
        return httpx.Response(200, json=_ISSUE)
    _jira(monkeypatch, handler)
    issue = asyncio.run(jira.get_issue("ABC-1"))
    assert issue["comments"] == []
    assert "could not be read" in issue["comments_unavailable"]


def test_a_missing_issue_is_a_clear_answer_not_a_traceback(monkeypatch):
    _jira(monkeypatch, lambda req: httpx.Response(404, json={}))
    with pytest.raises(ValueError, match="no issue ABC-9 that this account can see"):
        asyncio.run(jira.get_issue("ABC-9"))


def test_comments_still_arrive_when_both_calls_work(monkeypatch):
    def handler(req):
        if req.url.path.endswith("/comment"):
            return httpx.Response(200, json={"total": 1, "comments": [
                {"author": {"displayName": "R"}, "created": "x",
                 "body": {"type": "doc", "content": [{"type": "text", "text": "ok"}]}}]})
        return httpx.Response(200, json=_ISSUE)
    _jira(monkeypatch, handler)
    issue = asyncio.run(jira.get_issue("ABC-1"))
    assert issue["comment_total"] == 1 and "comments_unavailable" not in issue


# --- /api/_invoke: a failing tool is a clean, counted error ----------------

def test_a_failing_capability_is_a_502_with_its_reason(monkeypatch):
    import dataclasses
    from app import capabilities, main

    async def boom(**kw):
        raise RuntimeError("Element is not attached to the DOM")

    real = capabilities.get("list_background_tasks")
    broken = dataclasses.replace(real, fn=boom)
    monkeypatch.setattr(capabilities, "get",
                        lambda name: broken if name == "list_background_tasks" else real)
    monkeypatch.delenv("ASTA_TOKEN", raising=False)

    async def call():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/api/_invoke", json={"tool": "list_background_tasks", "args": {}})

    r = asyncio.run(call())
    assert r.status_code == 502
    assert "list_background_tasks failed" in r.text and "not attached to the DOM" in r.text
    rows = [o for o in store.recent_outcomes(10) if o["kind"] == "capability"]
    assert rows and rows[0]["subject"] == "list_background_tasks"


# --- MCP OAuth never opens a browser from the server ------------------------

def test_the_server_oauth_raises_instead_of_opening_a_browser(monkeypatch):
    import webbrowser
    from app import mcp_loader
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url))
    cls = mcp_loader.headless_oauth_class()
    auth = cls.__new__(cls)
    auth.server_name = "atlassian"
    with pytest.raises(mcp_loader.OAuthLoginRequired, match="app.mcp_login atlassian"):
        asyncio.run(auth.redirect_handler("https://auth.example/authorize?x=1"))
    assert opened == []


def test_the_login_command_keeps_the_interactive_flow():
    from pathlib import Path
    assert "interactive=True" in Path("app/mcp_login.py").read_text()


def test_a_failed_handshake_says_why_even_when_the_error_is_blank():
    from app import main
    assert main._handshake_reason(TimeoutError()) == "TimeoutError: no answer within 20s"
    group = ExceptionGroup("tg", [ValueError("401 unauthorized")])
    assert main._handshake_reason(group) == "ValueError: 401 unauthorized"


def test_health_names_a_dead_mcp_login_with_the_fix(monkeypatch):
    from app import health
    store.kv_set("mcp_handshake_failed", json.dumps({"atlassian": "TimeoutError: no answer within 20s"}))
    monkeypatch.setattr(health, "_is_oauth_server", lambda name: True)
    problem = health.mcp_problems()["mcp_atlassian"]
    assert "is off" in problem and "python -m app.mcp_login atlassian" in problem


# --- timestamps --------------------------------------------------------------

def test_log_lines_carry_a_timestamp():
    from app import logsetup
    logsetup.apply()
    stamped = [h for h in logging.getLogger().handlers if getattr(h, "_asta_stamped", False)]
    assert len(stamped) == 1
    assert "%(asctime)s" in stamped[0].formatter._fmt
    logsetup.apply()                      # idempotent: no second handler
    assert len([h for h in logging.getLogger().handlers
                if getattr(h, "_asta_stamped", False)]) == 1


def test_uvicorn_lines_are_stamped_too():
    from uvicorn.config import LOGGING_CONFIG
    import logging.config
    from app import logsetup
    logging.config.dictConfig(LOGGING_CONFIG)
    logsetup.apply()
    for name in ("uvicorn", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            assert "%(asctime)s" in h.formatter._fmt, name


# --- a limit that lifted is crossed out -------------------------------------

def test_an_exhausted_flag_stops_being_reported_once_its_cooldown_passes():
    """12 Sep: the scorecard said Copilot was up, health said "out for the
    billing period". The flag was set when it happened and never crossed out."""
    from app import agent
    agent.mark_quota_down("copilot", COPILOT_OUT)
    assert agent.quota_exhausted("copilot") is True
    store.kv_set(agent.quota_kv("copilot"), "1")            # cool-down long served
    assert agent.quota_down("copilot") is False             # which clears the marker
    assert agent.quota_exhausted("copilot") is False


def test_a_successful_call_crosses_every_limit_out():
    from app import agent
    agent.mark_quota_down("claude_cli", LIMIT)
    assert agent.quota_down("claude_cli")
    agent.mark_quota_ok("claude_cli")
    assert not agent.quota_down("claude_cli") and not agent.quota_exhausted("claude_cli")
    assert store.kv_get("claude_cli_quota_until") is None


def test_a_task_that_ran_clears_the_brains_limit(brains):
    from app import agent
    store.kv_set("claude_cli_quota_exhausted", "1")
    store.kv_set(agent.quota_kv("claude_cli"), "1")
    brains["script"]["claude"] = ["found it"]
    asyncio.run(tasks._worker(_analysis("claude")))
    assert agent.quota_exhausted("claude_cli") is False
