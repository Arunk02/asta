"""The worker credential boundary across three real processes (#177/#178).

The real API runs in its own process on a shared database file. A real
`python -m app.mcp_server` subprocess is started with the exact environment
`mcp_server.config_entry` gives a task, and is driven over stdio by an MCP
client — so every call goes MCP → HTTP → scope check → capability, as a worker's
does. Owner credentials never appear in the worker's environment.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest

from app import mcp_server, store, tasks, worker_auth
from tests.e2e.harness import ROOT, clean_env, server


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "e2e.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init()
    conv = store.create_conversation("claude", "booking")
    task = store.create_task("Investigate retry", "analysis", "check the retry", "booking")
    tasks.link_task(conv["id"], task["id"])
    store.update_task(task["id"], status="running", result="partial: retry cap missing")
    other = store.create_task("Someone else's task", "analysis", "secret work", "booking")
    store.update_task(other["id"], status="running", result="OTHER TASK RESULT")
    with server(db) as url:
        yield {"url": url, "db": db, "task": task, "other": other, "conv": conv}


def _worker_env(world, monkeypatch):
    port = world["url"].rsplit(":", 1)[1]
    monkeypatch.setenv("ASTA_PORT", port)
    monkeypatch.setenv("ASTA_URL", world["url"])
    monkeypatch.setenv("ASTA_TOKEN", "e2e-owner-token")
    with worker_auth.session(world["task"]["id"]) as (token, scope):
        cfg = mcp_server.config_entry(task_id=str(world["task"]["id"]), conv_id="forged-conv")
        env = cfg["mcpServers"]["asta"]["env"]
        yield token, env


async def _mcp(env: dict, calls):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    transport = StdioTransport(command=sys.executable, args=["-m", "app.mcp_server"],
                               env=clean_env(**env), cwd=str(ROOT))
    async with Client(transport) as client:
        return await calls(client)


def _text(result) -> str:
    parts = getattr(result, "content", None) or []
    return " ".join(getattr(p, "text", "") for p in parts) or str(getattr(result, "data", ""))


def test_a_worker_reaches_only_its_own_task_through_real_processes(world, monkeypatch):
    for token, env in _worker_env(world, monkeypatch):
        assert env["ASTA_TOKEN"] == token and "e2e-owner-token" not in json.dumps(env)

        async def calls(client):
            names = {t.name for t in await client.list_tools()}
            mine = await client.call_tool("task_result", {"task_id": world["task"]["id"]})
            theirs = await client.call_tool("task_result", {"task_id": world["other"]["id"]},
                                            raise_on_error=False)
            return names, _text(mine), theirs

        names, mine, theirs = asyncio.run(_mcp(env, calls))
        assert names == worker_auth.READ_TOOLS | worker_auth.STAGE_TOOLS
        assert "remember" not in names and "merge_pr" not in names and "delegate_task" not in names
        assert "retry cap missing" in mine
        assert theirs.is_error and "OTHER TASK RESULT" not in _text(theirs)


def test_the_worker_token_is_refused_everywhere_else_and_after_revocation(world, monkeypatch):
    url = world["url"]
    for token, env in _worker_env(world, monkeypatch):
        auth = {"Authorization": f"Bearer {token}"}
        assert httpx.get(f"{url}/api/memory/search", params={"q": "x"}, headers=auth).status_code == 401
        forged = httpx.post(f"{url}/api/_invoke", headers=auth,
                            json={"tool": "task_result", "args": {}, "task_id": world["other"]["id"]})
        assert forged.status_code == 403
        write = httpx.post(f"{url}/api/_invoke", headers=auth, json={"tool": "remember", "args": {}})
        assert write.status_code == 403
        live = httpx.post(f"{url}/api/_invoke", headers=auth,
                          json={"tool": "task_result", "args": {}})
        assert live.status_code == 200 and "retry cap missing" in json.dumps(live.json())
    # The session has ended: the same token no longer opens anything.
    gone = httpx.post(f"{url}/api/_invoke", headers={"Authorization": f"Bearer {token}"},
                      json={"tool": "task_result", "args": {}})
    assert gone.status_code == 401


def test_a_finished_task_closes_its_workers_door(world, monkeypatch):
    for token, env in _worker_env(world, monkeypatch):
        store.update_task(world["task"]["id"], status="done")
        r = httpx.post(f"{world['url']}/api/_invoke", headers={"Authorization": f"Bearer {token}"},
                       json={"tool": "task_result", "args": {}})
        assert r.status_code == 401


def test_the_owner_token_still_works_for_the_owner(world):
    r = httpx.get(f"{world['url']}/api/memory/search", params={"q": "retry"},
                  headers={"Authorization": "Bearer e2e-owner-token"})
    assert r.status_code == 200
