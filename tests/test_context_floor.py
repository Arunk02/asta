"""A chat session's start is small and its cached prefix stays valid (15 Nov).

Measured on the real command: a fresh chat started at 93k tokens, and 98 of 187
turns in two weeks rewrote 50-90k of cache because the system prompt changed
with each message. These pin the four causes shut.
"""

import asyncio
import json

import pytest

from app import claude_cli, copilot_cli, episodes, memory, mcp_server, store


def _sys(cmd):
    return cmd[cmd.index("--append-system-prompt") + 1]


def _msg(cmd):
    return cmd[cmd.index("-p") + 1]


@pytest.fixture
def conv(monkeypatch):
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    c = store.create_conversation(model="claude_cli", workspace=None)
    return c


def test_the_system_prompt_is_the_same_whatever_he_writes(conv):
    """The expert line and the file rule follow the message; they ride in it."""
    first = claude_cli._build_cmd(conv, "check the booking logs for 42 errors")
    second = claude_cli._build_cmd(conv, "make me an excel of the open PRs")
    assert _sys(first) == _sys(second)
    assert "THIS MESSAGE ASKS FOR A FILE" in _msg(second)
    assert "THIS MESSAGE ASKS FOR A FILE" not in _sys(second)


def test_a_remember_mid_session_does_not_change_the_system_prompt(conv, monkeypatch):
    before = claude_cli._build_cmd(conv, "hi")
    monkeypatch.setattr(memory, "index_text", lambda: "- a brand new memory line")
    after = claude_cli._build_cmd(conv, "hi again")
    assert _sys(before) == _sys(after)
    assert "brand new memory line" not in _sys(after)


def test_a_fresh_session_sees_the_new_memory_index(conv, monkeypatch):
    claude_cli._build_cmd(conv, "hi")
    store.kv_set(f"claude_session:{conv['id']}", "")          # retired
    monkeypatch.setattr(memory, "index_text", lambda: "- a brand new memory line")
    assert "brand new memory line" in _sys(claude_cli._build_cmd(conv, "hi"))


def test_a_light_read_never_replaces_the_main_sessions_snapshot(conv, monkeypatch):
    main = claude_cli._build_cmd(conv, "hi")
    monkeypatch.setattr(memory, "index_text", lambda: "- a brand new memory line")
    claude_cli._build_cmd(conv, "who messaged me today?")       # a light, one-off session
    assert _sys(claude_cli._build_cmd(conv, "and the next thing")) == _sys(main)


def test_the_recap_rides_in_the_first_message_once_not_in_the_system_prompt(conv):
    store.add_ui_message(conv["id"], "user", "look at task 7 please")
    store.add_ui_message(conv["id"], "assistant", "Task 7 is waiting on the CI fix.")
    store.kv_set(f"session_recap:{conv['id']}", "1")
    first = claude_cli._build_cmd(conv, "go on")
    assert "Conversation so far" in _msg(first)
    assert "Conversation so far" not in _sys(first)
    second = claude_cli._build_cmd(conv, "and then?")
    assert "Conversation so far" not in _msg(second)
    assert _sys(first) == _sys(second)


def test_a_fresh_session_is_told_what_is_still_in_flight(conv):
    """His question: does a new session start "without losing any data"?"""
    tid = store.create_task("Fix the equipment mapper", "code", "fix it", workspace=None)["id"]
    store.update_task(tid, status="awaiting_approval")
    store.add_ui_message(conv["id"], "user", "status?")
    store.add_ui_message(conv["id"], "assistant", "All good.")
    store.kv_set(f"session_recap:{conv['id']}", "1")
    msg = _msg(claude_cli._build_cmd(conv, "go on"))
    assert "Conversation so far" in msg
    assert f"task #{tid} awaiting_approval: Fix the equipment mapper" in msg


def test_copilot_turn_one_still_gets_everything():
    ctx = copilot_cli._first_turn_context({"id": "cop-1"}, user_text="make me an excel of PRs")
    assert "THIS MESSAGE ASKS FOR A FILE" in ctx and "AUTONOMOUS LOOP" in ctx


def test_hot_tools_are_pinned_and_the_rest_are_searchable(monkeypatch):
    monkeypatch.delenv("ASTA_TOOL_SEARCH", raising=False)
    for name in ("log_lines", "log_counts", "find_id", "grafana_logs", "prepare_to_send",
                 "delegate_task", "remember", "ask_user"):
        assert mcp_server.tool_meta(name) == {"anthropic/alwaysLoad": True}, name
    assert mcp_server.tool_meta("study_start") is None
    assert mcp_server.tool_meta("set_reminder") is None
    monkeypatch.setenv("ASTA_TOOL_SEARCH", "1")
    assert mcp_server.tool_meta("log_lines") is None


def test_the_server_carries_the_mark_to_the_cli(monkeypatch):
    monkeypatch.delenv("ASTA_TOOL_SEARCH", raising=False)
    tools = {t.name: t for t in asyncio.run(mcp_server.build_server().list_tools())}
    assert (tools["log_lines"].meta or {}).get("anthropic/alwaysLoad") is True
    assert not (tools["study_start"].meta or {}).get("anthropic/alwaysLoad")


def test_every_hot_tool_exists():
    from app import capabilities
    assert mcp_server.HOT <= set(capabilities.registry())


def test_tool_search_is_never_switched_off(monkeypatch):
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    assert "ENABLE_TOOL_SEARCH" not in claude_cli._subprocess_env()


def test_claude_codes_own_skill_catalogue_stays_out(conv):
    assert claude_cli.NO_SKILLS in claude_cli._build_cmd(conv, "hi")


def test_a_sitting_ends_when_the_cache_would_have_gone_cold(monkeypatch):
    monkeypatch.delenv("ASTA_EPISODE_GAP_MINUTES", raising=False)
    assert episodes.gap_seconds() == 3600


# --- task legs: only the tools a build uses (16 Nov) ----------------------------

def _leg_cmd(monkeypatch, version, **kw):
    seen = {}

    async def _exec(*cmd, **_k):
        seen["cmd"] = list(cmd)
        raise RuntimeError("captured")

    monkeypatch.setattr(claude_cli, "_version", version)
    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(RuntimeError, match="captured"):
        asyncio.run(claude_cli._one_shot("build it", "/tmp", 60, "", "", "sid", False, None,
                                         '{"mcpServers":{}}', False, "", False, **kw))
    return seen["cmd"]


def test_a_code_leg_carries_only_the_build_tools(monkeypatch):
    cmd = _leg_cmd(monkeypatch, (2, 1, 311), builtins=claude_cli.CODE_TOOLS)
    tools = cmd[cmd.index("--tools") + 1].split(",")
    assert {"Bash", "Read", "Edit", "Write", "ToolSearch"} <= set(tools)
    assert "WebFetch" not in tools and "NotebookEdit" not in tools
    assert claude_cli.SAME_PREFIX in cmd


def test_an_old_cli_gets_the_old_command(monkeypatch):
    cmd = _leg_cmd(monkeypatch, (2, 1, 212), builtins=claude_cli.CODE_TOOLS)
    assert "--tools" not in cmd and claude_cli.SAME_PREFIX not in cmd


def test_a_one_shot_that_names_no_tools_keeps_the_full_set(monkeypatch):
    cmd = _leg_cmd(monkeypatch, (2, 1, 311))
    assert "--tools" not in cmd


def test_the_code_leg_asks_for_the_build_tools(monkeypatch):
    from app import tasks
    t = store.create_task("x", "code", "p", None)["id"]
    seen = {}

    async def _cli(task_id, executor, prompt, **kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(tasks, "_observed_cli", _cli)
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    asyncio.run(tasks._code_leg(t, "build", "/tmp", resume=False, effort="high"))
    assert seen["builtins"] == claude_cli.CODE_TOOLS


def test_a_task_worker_pins_every_tool_its_scope_grants(monkeypatch):
    monkeypatch.setenv("ASTA_MCP_SCOPED", "1")
    assert mcp_server.tool_meta("report_outcome") == {"anthropic/alwaysLoad": True}


def test_a_task_pins_only_what_its_kind_uses_and_searches_the_rest(monkeypatch):
    """17 Nov: every leg pinned all 31 scoped tools (~5k tokens)."""
    monkeypatch.setenv("ASTA_MCP_SCOPED", "1")
    monkeypatch.setenv("ASTA_MCP_PIN", "grafana_logs,log_lines")
    assert mcp_server.tool_meta("grafana_logs") == {"anthropic/alwaysLoad": True}
    assert mcp_server.tool_meta("teams_history") is None
    monkeypatch.setenv("ASTA_MCP_PIN", "")              # inherited empty from .env
    assert mcp_server.tool_meta("teams_history") == {"anthropic/alwaysLoad": True}
    monkeypatch.delenv("ASTA_MCP_PIN")
    assert mcp_server.tool_meta("teams_history") == {"anthropic/alwaysLoad": True}


def test_investigations_keep_the_debug_kit_pinned():
    from app import tasks, worker_auth
    kit = set(tasks.PINNED_TOOLS["analysis"])
    assert {"grafana_logs", "log_lines", "log_counts", "find_id", "temporal_workflow"} <= kit
    for kind, names in tasks.PINNED_TOOLS.items():
        assert set(names) <= worker_auth.READ_TOOLS | worker_auth.STAGE_TOOLS, kind


def test_the_pin_reaches_the_worker_config(monkeypatch):
    import json
    from app import claude_cli, store, tasks, worker_auth
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    monkeypatch.setenv("ASTA_DEV_MCP", "")
    monkeypatch.setattr(claude_cli, "on_demand", lambda: True)
    t = store.create_task("check booking", "analysis", "p", None)
    with worker_auth.session(t["id"]):
        env = json.loads(tasks.task_tools(t["id"], "/tmp/ws", "analysis"))["mcpServers"]["asta"]["env"]
    assert env["ASTA_MCP_PIN"].split(",") == list(tasks.PINNED_TOOLS["analysis"])
    monkeypatch.setattr(claude_cli, "on_demand", lambda: False)
    with worker_auth.session(t["id"]):
        env = json.loads(tasks.task_tools(t["id"], "/tmp/ws", "analysis"))["mcpServers"]["asta"]["env"]
    assert "ASTA_MCP_PIN" not in env
