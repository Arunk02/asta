"""Dev MCP servers (Serena + Context7) reach the code-task brains — and nothing else.

Two things must hold, forever: off by default it changes the command not at all
(so no code task can start behaving differently until Arun flips the flag), and a
missing binary is skipped rather than crashing a task. The wiring tests drive the
real task legs and assert the brain was handed exactly the config the policy built.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import claude_cli, copilot_cli, dev_mcp, store, tasks


# --- policy: what servers, when -------------------------------------------------

def test_disabled_is_a_pure_noop(monkeypatch):
    monkeypatch.delenv("ASTA_DEV_MCP", raising=False)
    monkeypatch.setattr(dev_mcp, "_on_path", lambda c: True)      # even if installed
    assert dev_mcp.config_for("/repo") is None
    assert dev_mcp.config_json("/repo") == ""


def test_enabled_attaches_both_when_installed(monkeypatch):
    monkeypatch.setenv("ASTA_DEV_MCP", "1")
    monkeypatch.setattr(dev_mcp, "_on_path", lambda c: True)
    cfg = dev_mcp.config_for("/work/contmark")
    assert set(cfg["mcpServers"]) == {"serena", "context7"}
    # Serena must be pointed at THIS repo, or it indexes the wrong tree.
    assert cfg["mcpServers"]["serena"]["args"][-2:] == ["--project", "/work/contmark"]


def test_a_missing_binary_is_skipped_never_fatal(monkeypatch):
    monkeypatch.setenv("ASTA_DEV_MCP", "1")
    # Only context7 installed; serena absent.
    monkeypatch.setattr(dev_mcp, "_on_path",
                        lambda c: "context7" in c)
    cfg = dev_mcp.config_for("/repo")
    assert set(cfg["mcpServers"]) == {"context7"}


def test_nothing_installed_means_no_config(monkeypatch):
    monkeypatch.setenv("ASTA_DEV_MCP", "1")
    monkeypatch.setattr(dev_mcp, "_on_path", lambda c: False)
    assert dev_mcp.config_for("/repo") is None
    assert dev_mcp.config_json("/repo") == ""


def test_no_workspace_means_no_config(monkeypatch):
    monkeypatch.setenv("ASTA_DEV_MCP", "1")
    monkeypatch.setattr(dev_mcp, "_on_path", lambda c: True)
    assert dev_mcp.config_for("") is None


def test_commands_are_env_overridable(monkeypatch):
    monkeypatch.setenv("ASTA_DEV_MCP", "1")
    monkeypatch.setenv("ASTA_SERENA_CMD", "/opt/serena/bin/serena")
    monkeypatch.setenv("ASTA_CONTEXT7_CMD", "my-c7")
    monkeypatch.setattr(dev_mcp, "_on_path", lambda c: True)
    devs = dev_mcp.servers("/repo")
    assert devs["serena"]["command"] == "/opt/serena/bin/serena"
    assert devs["context7"]["command"] == "my-c7"


def test_dev_servers_merge_over_a_base_config(monkeypatch):
    monkeypatch.setenv("ASTA_DEV_MCP", "1")
    monkeypatch.setattr(dev_mcp, "_on_path", lambda c: True)
    base = {"mcpServers": {"asta": {"command": "python"}}}
    cfg = dev_mcp.config_for("/repo", base)
    assert set(cfg["mcpServers"]) == {"asta", "serena", "context7"}
    assert cfg["mcpServers"]["asta"] == {"command": "python"}      # base untouched


def test_config_json_round_trips(monkeypatch):
    monkeypatch.setenv("ASTA_DEV_MCP", "1")
    monkeypatch.setattr(dev_mcp, "_on_path", lambda c: True)
    parsed = json.loads(dev_mcp.config_json("/repo"))
    assert set(parsed["mcpServers"]) == {"serena", "context7"}


# --- wiring: the brains actually receive it ------------------------------------

def _capture_one_shot(monkeypatch, module):
    """Replace a brain's one_shot with a fake that records how it was called and
    returns a finished result — overriding conftest's no-live-brains guard."""
    seen: dict = {}

    async def fake(prompt, **kw):
        seen.update(kw)
        return "done: diff summary"

    monkeypatch.setattr(module, "one_shot", fake)
    return seen


@pytest.mark.parametrize("executor,module", [("copilot", copilot_cli),
                                             ("claude", claude_cli)])
def test_code_leg_hands_dev_mcp_to_whichever_brain_runs(monkeypatch, executor, module):
    monkeypatch.setenv("ASTA_DEV_MCP", "1")
    monkeypatch.setattr(dev_mcp, "_on_path", lambda c: True)
    seen = _capture_one_shot(monkeypatch, module)

    t = store.create_task("fix the retry", "code", "fix the retry timeout", None)
    store.kv_set(f"task_executor:{t['id']}", executor)
    asyncio.run(tasks._run_code_leg(t["id"], "fix the retry timeout",
                                    "/work/contmark", resume=False, effort="medium"))

    cfg = json.loads(seen["mcp_config"])
    assert set(cfg["mcpServers"]) == {"serena", "context7"}
    assert cfg["mcpServers"]["serena"]["args"][-1] == "/work/contmark"


def test_code_leg_passes_empty_config_when_disabled(monkeypatch):
    monkeypatch.delenv("ASTA_DEV_MCP", raising=False)
    seen = _capture_one_shot(monkeypatch, copilot_cli)

    t = store.create_task("small edit", "code", "tweak the log line", None)
    store.kv_set(f"task_executor:{t['id']}", "copilot")
    asyncio.run(tasks._run_code_leg(t["id"], "tweak the log line",
                                    "/work/contmark", resume=False, effort="medium"))
    assert seen["mcp_config"] == ""                     # command byte-for-byte unchanged


def test_analysis_leg_gets_dev_mcp_but_teams_draft_does_not(monkeypatch):
    monkeypatch.setenv("ASTA_DEV_MCP", "1")
    monkeypatch.setattr(dev_mcp, "_on_path", lambda c: True)

    seen_analysis = _capture_one_shot(monkeypatch, copilot_cli)
    t1 = store.create_task("look at the dispatch path", "analysis",
                           "how does dispatch work", None)
    store.kv_set(f"task_executor:{t1['id']}", "copilot")
    asyncio.run(tasks._run_simple(t1["id"], store.get_task(t1["id"]),
                                  "how does dispatch work"))
    assert json.loads(seen_analysis["mcp_config"])["mcpServers"]

    seen_draft = _capture_one_shot(monkeypatch, copilot_cli)
    t2 = store.create_task("reply to Vinish", "teams_draft", "draft a reply", None)
    store.kv_set(f"task_executor:{t2['id']}", "copilot")
    asyncio.run(tasks._run_simple(t2["id"], store.get_task(t2["id"]), "draft a reply"))
    assert seen_draft["mcp_config"] == ""               # non-code kinds get nothing
