"""The MCP foundation that lets CLI brains drop the curl round-trip.

Design under test: capabilities run in the LIVE server process via /api/_invoke,
and Asta's MCP server forwards each native tool call there. This is what keeps
stateful tools (delegate_task's worker, ask_user's future) working — they'd break
if the MCP subprocess ran them itself.
"""

from __future__ import annotations

import asyncio
import json
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


def test_claude_orientation_is_a_system_prompt_not_user_text(monkeypatch):
    """The refusal bug: Asta's identity/rules delivered inside the -p user message
    read as untrusted injection to a strong model, which then answered as plain
    Claude Code and refused to use Asta's capabilities. It must ride in
    --append-system-prompt (authoritative), with the -p message left as Arun's
    actual words."""
    from app import claude_cli
    cmd = claude_cli._build_cmd({"id": "sysprompt"}, "any messages for me?")
    assert "--append-system-prompt" in cmd
    system = cmd[cmd.index("--append-system-prompt") + 1]
    prompt = cmd[cmd.index("-p") + 1]
    # identity + rules live in the SYSTEM prompt
    assert "Asta" in system and "AUTONOMOUS LOOP" in system
    # the user message is Arun's words, not the orientation manual
    assert "any messages for me?" in prompt
    assert "AUTONOMOUS LOOP" not in prompt and "You are acting as" not in prompt


def test_allowed_tools_narrows_but_never_drops_the_floor(monkeypatch):
    from app import mcp_server, capabilities
    monkeypatch.delenv("ASTA_MCP_TOOLS", raising=False)
    assert mcp_server._allowed_tools() is None                 # unset -> full set
    monkeypatch.setenv("ASTA_MCP_TOOLS", "teams_activity,jira_issue")
    allow = mcp_server._allowed_tools()
    assert "teams_activity" in allow and "jira_issue" in allow
    assert set(capabilities.ALWAYS) <= allow                   # floor folded in unconditionally


def test_config_entry_carries_selected_tools_only_when_given():
    from app import mcp_server
    plain = mcp_server.config_entry()["mcpServers"]["asta"]["env"]
    assert "ASTA_MCP_TOOLS" not in plain                        # full set by default
    narrowed = mcp_server.config_entry(tools=["teams_activity", "remember"])
    assert narrowed["mcpServers"]["asta"]["env"]["ASTA_MCP_TOOLS"] == "teams_activity,remember"


def test_claude_command_passes_the_selected_tools_inline(monkeypatch):
    """The wiring: whatever the selector picks rides into the spawned MCP server as
    ASTA_MCP_TOOLS. Passed as INLINE JSON (no file on disk) — claude --mcp-config
    takes strings, matching the Copilot path and leaving nothing to clean up."""
    from app import claude_cli, tool_index
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    monkeypatch.setattr(tool_index, "select_sticky", lambda cid, q, k=8: ["teams_activity", "remember"])
    cmd = claude_cli._build_cmd({"id": "ctx1"}, "any teams messages?")
    arg = cmd[cmd.index("--mcp-config") + 1]
    assert not arg.endswith(".json")                           # inline JSON, not a file path
    env = json.loads(arg)["mcpServers"]["asta"]["env"]         # parses as JSON
    assert env["ASTA_MCP_TOOLS"] == "teams_activity,remember"


def test_claude_command_exposes_all_tools_when_context_is_ambiguous(monkeypatch):
    from app import claude_cli, tool_index
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    monkeypatch.setattr(tool_index, "select_sticky", lambda cid, q, k=8: None)   # ambiguous
    cmd = claude_cli._build_cmd({"id": "ctx2"}, "hmm")
    env = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["asta"]["env"]
    assert "ASTA_MCP_TOOLS" not in env                          # None -> full set, safe default


def test_conversational_turns_get_only_the_floor(monkeypatch):
    """Tier 0: a pure reasoning turn that ranks no tool gets the ALWAYS floor, not
    all 50 — the biggest token lever."""
    from app import tool_index, capabilities
    monkeypatch.setattr(tool_index, "select", lambda q, k=8: None)   # nothing ranked
    floor = set(capabilities.ALWAYS) & set(capabilities.registry())
    assert set(tool_index.select_sticky("c-conv", "explain that in more detail")) == floor
    # ranks nothing but ISN'T conversational -> full set (never strand a real ask)
    assert tool_index.select_sticky("c-vague", "do the thing we discussed") is None


def test_is_conversational_is_deliberately_tight():
    from app import tool_index
    for yes in ("explain that", "why did you do that", "elaborate",
                "what do you mean", "summarise the above", "walk me through it",
                "compare the two approaches", "help me understand this",
                "what's the difference", "tldr", "what do you think about that"):
        assert tool_index.is_conversational(yes), yes
    for no in ("send a message to vinish", "any teams messages for me",
               "create a task to fix the bug", "what's in my sprint",
               "raise a PR for the fix", "check CI status"):
        assert not tool_index.is_conversational(no), no


class _Stop(Exception):
    pass


def test_prefetch_is_skipped_under_mcp_kept_without_it(monkeypatch):
    """MCP-era: the brain reads Teams/Outlook via native tools, so the ~25s
    pre-read is skipped. MCP-off: the pre-read stays (the brain can't reach them
    itself). Bails before spawning any real CLI."""
    from app import copilot_cli
    calls = {"prefetch": 0}
    captured = {}

    async def fake_prefetch(text):
        calls["prefetch"] += 1
        return "TEAMS CONTEXT"

    def fake_build(conv, user_text, extra_context=""):
        captured["ctx"] = extra_context
        raise _Stop()

    monkeypatch.setattr(copilot_cli, "available", lambda: True)
    monkeypatch.setattr(copilot_cli, "_prefetch", fake_prefetch)
    monkeypatch.setattr(copilot_cli, "_build_cmd", fake_build)

    monkeypatch.setenv("ASTA_CLI_MCP", "1")                 # MCP on -> no pre-read
    with pytest.raises(_Stop):
        asyncio.run(copilot_cli.run_turn({"id": "x"}, "any teams messages?", None))
    assert calls["prefetch"] == 0 and captured["ctx"] == ""

    monkeypatch.setenv("ASTA_CLI_MCP", "0")                 # MCP off -> pre-read kept
    with pytest.raises(_Stop):
        asyncio.run(copilot_cli.run_turn({"id": "x"}, "any teams messages?", None))
    assert calls["prefetch"] == 1 and captured["ctx"] == "TEAMS CONTEXT"


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
