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
