"""The MCP foundation that lets CLI brains drop the curl round-trip.

Design under test: capabilities run in the LIVE server process via /api/_invoke,
and Asta's MCP server forwards each native tool call there. This is what keeps
stateful tools (delegate_task's worker, ask_user's future) working — they'd break
if the MCP subprocess ran them itself.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from app import capabilities, store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db")
    store.init()
    monkeypatch.setenv("ASTA_TOKEN", "tkn")
    monkeypatch.setenv("TEAMS_BRIDGE", "0")
    from app import main as m
    transport = httpx.ASGITransport(app=m.app)
    return httpx.AsyncClient(transport=transport, base_url="http://t",
                             headers={"Authorization": "Bearer tkn"})


def test_invoke_runs_a_capability_by_name(client):
    async def go():
        async with client as c:
            r = await c.post("/api/_invoke",
                             json={"tool": "list_background_tasks", "args": {}})
            assert r.status_code == 200
            assert "result" in r.json()
    asyncio.run(go())


def test_invoke_unknown_tool_404s(client):
    async def go():
        async with client as c:
            r = await c.post("/api/_invoke", json={"tool": "no_such_tool", "args": {}})
            assert r.status_code == 404
    asyncio.run(go())


def test_invoke_bad_args_400s_not_500s(client):
    async def go():
        async with client as c:
            r = await c.post("/api/_invoke",
                             json={"tool": "jira_issue", "args": {"wrong": "x"}})
            assert r.status_code == 400
    asyncio.run(go())


def test_invoke_requires_auth(client):
    async def go():
        async with client as c:
            r = await c.post("/api/_invoke", json={"tool": "list_background_tasks"},
                             headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401
    asyncio.run(go())


def test_mcp_server_exposes_every_capability_with_its_schema():
    """Each proxy must carry cap.fn's own signature, or the brain gets a tool it
    cannot call correctly."""
    from app import mcp_server
    srv = mcp_server.build_server()
    tools = asyncio.run(srv.list_tools())
    assert len(tools) == len(capabilities.registry())
    by_name = {t.name: t for t in tools}
    # a few known signatures survive the proxy wrapping
    assert set(by_name["set_reminder"].inputSchema["properties"]) == {
        "text", "due_iso", "repeat"}
    assert by_name["jira_issue"].inputSchema["required"] == ["key"]


def test_config_carries_token_and_is_not_world_readable(tmp_path, monkeypatch):
    """The config file holds the bearer token, so it must be 0600 and under data/."""
    from app import mcp_server
    monkeypatch.setenv("ASTA_TOKEN", "sekret")
    p = mcp_server.write_config(tmp_path / "asta-mcp.json")
    import json
    entry = json.loads(p.read_text())["mcpServers"]["asta"]
    assert entry["env"]["ASTA_TOKEN"] == "sekret"
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_cli_mcp_is_off_by_default_and_opt_in(monkeypatch):
    """The cutover must be off by default — the curl path stays the proven one
    until a real-turn test flips it on."""
    from app import copilot_cli
    monkeypatch.delenv("ASTA_CLI_MCP", raising=False)
    assert copilot_cli.mcp_cli_enabled() is False
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    assert copilot_cli.mcp_cli_enabled() is True


def test_flag_swaps_the_claude_command_and_orientation(monkeypatch):
    from app import claude_cli, copilot_cli
    monkeypatch.setenv("ASTA_CLI_MCP", "0")
    assert "--mcp-config" not in claude_cli._build_cmd({"id": "a"}, "hi")
    ctx_off = copilot_cli._first_turn_context({"id": "a"}, user_text="hi")
    assert "curl" not in ctx_off or "native MCP tools" not in ctx_off
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    cmd = claude_cli._build_cmd({"id": "b"}, "hi")
    assert "--mcp-config" in cmd and "--strict-mcp-config" in cmd
    ctx_on = copilot_cli._first_turn_context({"id": "b"}, user_text="hi")
    assert "native MCP tools" in ctx_on
    # the ~2k-token curl catalogue is gone when MCP is on
    assert len(ctx_on) < len(ctx_off)


def test_mcp_proxy_forwards_to_invoke(monkeypatch):
    """A tool call must POST {tool,args} to /api/_invoke, not run the function
    here — that is the whole point of the seam."""
    from app import mcp_server
    sent = {}

    class FakeResp:
        status_code = 200
        def json(self): return {"result": "ok"}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json, headers):
            sent["url"] = url
            sent["payload"] = json
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    cap = capabilities.get("health_check")
    proxy = mcp_server._proxy(cap)
    out = asyncio.run(proxy())
    assert out == "ok"
    assert sent["url"].endswith("/api/_invoke")
    assert sent["payload"] == {"tool": "health_check", "args": {}}
