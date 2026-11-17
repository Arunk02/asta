"""A turn that debugs a case can reach the code and the evidence (17 Nov).

Three things stood between the chat and the answer that day:
  * every log and code tool was one search away in the on-demand chat;
  * the shell refused any read outside Asta's own folder (measured: ls, cat and
    head on the booking workspace refused; fine once the folder was added), and
    `git -C` / `cd … && git` are refused by the CLI itself;
  * log lines were cut at 320 characters, and the Event History categories that
    answered the question sat past the cut.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess

import pytest

from app import agent, booking_case, claude_cli, debugkit, mcp_server, store


@pytest.mark.parametrize("text", [
    "Bro, for this booking, can you see from VTS? I think we got the status as pending, "
    "but still, it's processed.",
    "MXAAAA1111BB",
    "go and confirm why the EH was added for ocean",
    "check BEPTELIKOS-12062",
])
def test_a_case_or_a_why_is_an_investigation(text):
    assert booking_case.investigating(text)


@pytest.mark.parametrize("text", ["remind me tomorrow to chase the PRs",
                                  "make me an excel of my meetings"])
def test_small_talk_is_not(text):
    assert not booking_case.investigating(text)


@pytest.fixture
def conv(monkeypatch):
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    monkeypatch.setattr(claude_cli, "_version", (2, 1, 311))
    monkeypatch.setattr(claude_cli, "workspace_roots", lambda: ["/tmp/ws-booking"])
    return store.create_conversation(model="claude_cli", workspace=None)


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def _mcp_env(cmd):
    return json.loads(_flag(cmd, "--mcp-config"))["mcpServers"]["asta"]["env"]


def test_an_investigation_turn_has_its_kit_in_front(conv):
    cmd = claude_cli._build_cmd(conv, "why did MXAAAA1111BB go to TMS while VTS was pending")
    assert _flag(cmd, "--plugin-dir") == str(claude_cli.ON_DEMAND_INVESTIGATE_PLUGIN)
    assert _mcp_env(cmd).get("ASTA_CHAT_INVESTIGATE") == "1"


def test_small_talk_keeps_the_light_chat(conv):
    cmd = claude_cli._build_cmd(conv, "remind me tomorrow to chase the PRs")
    assert _flag(cmd, "--plugin-dir") == str(claude_cli.ON_DEMAND_CHAT_PLUGIN)
    assert "ASTA_CHAT_INVESTIGATE" not in _mcp_env(cmd)


def test_the_registered_workspace_is_inside_the_chat(conv):
    cmd = claude_cli._build_cmd(conv, "hi")
    assert _flag(cmd, "--add-dir") == "/tmp/ws-booking"


def test_the_investigate_plugin_keeps_the_reading_tools_in_front():
    js = (claude_cli.ON_DEMAND_INVESTIGATE_PLUGIN / "hooks" / "register.js").read_text()
    for tool in ("Read", "Grep", "Glob", "Bash", "ToolSearch"):
        assert f'"{tool}"' in js
    assert (claude_cli.ON_DEMAND_INVESTIGATE_PLUGIN / ".claude-plugin" / "plugin.json").exists()


def test_the_kit_is_pinned_even_when_everything_else_is_searched(monkeypatch):
    monkeypatch.setenv("ASTA_TOOL_SEARCH", "1")
    monkeypatch.delenv("ASTA_MCP_SCOPED", raising=False)
    assert mcp_server.tool_meta("log_lines") is None
    monkeypatch.setenv("ASTA_CHAT_INVESTIGATE", "1")
    for name in ("log_lines", "grafana_logs", "code_history", "jira_issue", "teams_read_chat"):
        assert mcp_server.tool_meta(name) == {"anthropic/alwaysLoad": True}, name
    assert mcp_server.tool_meta("set_reminder") is None


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_code_history_reads_the_registered_checkout_and_says_which(tmp_path, monkeypatch):
    repo = tmp_path / "telikos-booking-service"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    (repo / "Mapper.java").write_text("class Mapper {}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "Add the ocean mapper")
    from app import workspace
    monkeypatch.setattr(workspace, "_root", lambda name: tmp_path)

    out = asyncio.run(agent.code_history("booking", "telikos-booking-service", "Mapper.java"))
    assert out.splitlines()[0].startswith("telikos-booking-service @ develop ")
    assert "Add the ocean mapper" in out and "Dev" in out

    blame = asyncio.run(agent.code_history("booking", "telikos-booking-service", "Mapper.java",
                                           what="blame", line_from=1))
    assert "class Mapper" in blame
    assert "No repo 'nope'" in asyncio.run(agent.code_history("booking", "nope"))


def _row(msg):
    now = dt.datetime(2026, 11, 17, 10, 11, 12, tzinfo=dt.timezone.utc).timestamp()
    return {"ts": now, "pod": "booking-service-abc-phm9j", "app": "booking-service",
            "logger": "EventDispatcher", "message": msg}


def test_full_prints_a_payload_whole(monkeypatch):
    payload = "EventHistory published result: " + ("x" * 900) + " Ocean transportation details"

    async def lines(*a, **k):
        return {"rows": [_row(payload)], "namespace": "telikos-sit", "link": "L",
                "start": 0.0, "end": 0.0, "query": "q"}

    monkeypatch.setattr(debugkit, "lines", lines)
    cut = asyncio.run(agent.log_lines("sit", contains=["JXBBBB2222CC"]))
    whole = asyncio.run(agent.log_lines("sit", contains=["JXBBBB2222CC"], full=True))
    assert "Ocean transportation details" not in cut
    assert "Ocean transportation details" in whole


def test_a_local_time_given_as_utc_is_pointed_out(monkeypatch):
    calls = []

    async def lines(env, service, terms, logger, s, e, limit, order, pod):
        calls.append((s, e))
        rows = [_row("found")] if len(calls) > 1 else []
        return {"rows": rows, "namespace": "telikos-sit", "link": "L",
                "start": s.timestamp(), "end": e.timestamp(), "query": "q"}

    monkeypatch.setattr(debugkit, "lines", lines)
    monkeypatch.setattr(agent._dt, "datetime", _FixedOffset)
    out = asyncio.run(agent.log_lines("sit", contains=["X"], at="2026-11-17T14:12:41+00:00"))
    assert "No lines" in out and "local time as UTC" in out and "08:42:41Z" in out


class _FixedOffset(dt.datetime):
    """IST, whatever zone the test machine runs in."""

    @classmethod
    def now(cls, tz=None):
        return dt.datetime(2026, 11, 17, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
