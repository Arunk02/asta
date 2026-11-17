"""ASTA_ON_DEMAND: a chat loads nothing up front and fetches what a turn needs (16 Nov).

His words: "it will automatically fetch what is required instead of we
restricting". Nothing is cut — every tool stays reachable by name.
"""

import json
import shutil
import subprocess
import textwrap

import pytest

from app import agent, claude_cli, copilot_cli, memory, skills, store


@pytest.fixture
def conv(monkeypatch):
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    monkeypatch.setattr(claude_cli, "_version", (2, 1, 311))
    return store.create_conversation(model="claude_cli", workspace=None)


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_off_by_default_the_command_is_the_classic_one(conv):
    cmd = claude_cli._build_cmd(conv, "hi")
    assert "--append-system-prompt" in cmd and "--system-prompt" not in cmd
    assert "--plugin-dir" not in cmd


def test_on_asta_writes_the_whole_prompt_and_nothing_is_cut(conv, monkeypatch):
    monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    cmd = claude_cli._build_cmd(conv, "remind me tomorrow to chase the PRs")
    assert "--append-system-prompt" not in cmd
    system = _flag(cmd, "--system-prompt")
    assert "You are Asta" in system and "ToolSearch" in system
    assert "--tools" not in cmd, "on demand nothing is restricted"
    assert _flag(cmd, "--setting-sources") == ""
    plugin = _flag(cmd, "--plugin-dir")
    assert plugin == str(claude_cli.ON_DEMAND_CHAT_PLUGIN)
    env = json.loads(_flag(cmd, "--mcp-config"))["mcpServers"]["asta"]["env"]
    assert env["ASTA_TOOL_SEARCH"] == "1", "every Asta tool searchable too"


def test_an_older_cli_gets_the_classic_command_whatever_the_flag(conv, monkeypatch):
    monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    monkeypatch.setattr(claude_cli, "_version", (2, 1, 267))
    assert "--system-prompt" not in claude_cli._build_cmd(conv, "hi")


def test_the_prompt_is_the_same_every_turn_and_keeps_his_rules(conv, monkeypatch):
    monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    a = _flag(claude_cli._build_cmd(conv, "check the booking logs for errors"), "--system-prompt")
    monkeypatch.setattr(memory, "index_text", lambda: "- a brand new memory line")
    b = _flag(claude_cli._build_cmd(conv, "make me an excel of the open PRs"), "--system-prompt")
    assert a == b
    from app import guardrails
    rules = guardrails.block("chat")
    assert not rules or rules in a
    assert f'"{conv["id"]}"' in a and "continue_working" in a and "prepare_to_send" in a


def test_the_code_work_flow_is_a_playbook_not_prompt_text(conv, monkeypatch):
    monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    system = _flag(claude_cli._build_cmd(conv, "hi"), "--system-prompt")
    assert "code-work" in system
    assert "DELEGATE, do not interrogate" not in system
    assert skills.load("code-work") == copilot_cli.CODE_WORK_PLAYBOOK
    assert "code-work" in agent.load_skill("no-such-skill")


def test_the_classic_chat_still_carries_the_code_work_flow(conv):
    assert "DELEGATE, do not interrogate" in _flag(claude_cli._build_cmd(conv, "hi"),
                                                   "--append-system-prompt")


def test_playbooks_are_named_not_described(conv, monkeypatch, tmp_path):
    monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    (tmp_path / "triage.md").write_text("---\nname: triage\ndescription: a long one-liner "
                                        "that would cost tokens on every call\n---\nbody")
    monkeypatch.setenv("ASTA_SKILL_PATHS", str(tmp_path))
    system = _flag(claude_cli._build_cmd(conv, "hi"), "--system-prompt")
    assert "triage" in system and "would cost tokens" not in system


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_plugin_defers_every_built_in_but_the_search(tmp_path):
    mod = tmp_path / "register.mjs"
    shutil.copy(claude_cli.ON_DEMAND_CHAT_PLUGIN / "hooks" / "register.js", mod)
    js = textwrap.dedent(f"""
        import {{ register }} from {json.dumps(str(mod))};
        let hook;
        register((event, h) => {{ if (event === "tool.describe") hook = h; }});
        const out = {{}};
        for (const tool of ["Bash", "Read", "WebFetch", "Task", "ToolSearch", "mcp__asta__remember"]) {{
          const e = {{ tool, description: "d" }};
          const r = hook(null, e, (x) => ({{ ...x, passed: true }}));
          out[tool] = r.isDeferred === true ? "deferred" : (r.passed ? "untouched" : "?");
        }}
        console.log(JSON.stringify(out));
    """)
    out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"Bash": "deferred", "Read": "deferred",
                                      "WebFetch": "deferred", "Task": "deferred",
                                      "ToolSearch": "untouched", "mcp__asta__remember": "untouched"}


def test_the_plugin_is_a_valid_claude_code_plugin():
    root = claude_cli.ON_DEMAND_CHAT_PLUGIN
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    hooks = json.loads((root / "hooks" / "hooks.json").read_text())
    assert manifest["name"] and hooks["modules"] == ["./register.js"]
    assert (root / "hooks" / "register.js").is_file()


def test_a_parallel_read_only_answer_is_told_so(conv, monkeypatch):
    from app import capabilities
    monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    token = capabilities.READ_ONLY_TURN.set(True)
    try:
        system = _flag(claude_cli._build_cmd(conv, "status?"), "--system-prompt")
    finally:
        capabilities.READ_ONLY_TURN.reset(token)
    assert "parallel read-only answer" in system


# --- code legs (16 Nov) -------------------------------------------------------

def _leg(monkeypatch, on, version=(2, 1, 311)):
    import asyncio
    seen = {}

    async def _exec(*cmd, **_k):
        seen["cmd"] = list(cmd)
        raise RuntimeError("captured")

    if on:
        monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    monkeypatch.setattr(claude_cli, "_version", version)
    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(RuntimeError, match="captured"):
        asyncio.run(claude_cli._one_shot("build it", "/tmp", 60, "", "", "sid", False, None,
                                         '{"mcpServers":{}}', False, "", False,
                                         builtins=claude_cli.CODE_TOOLS))
    return seen["cmd"]


def test_a_code_leg_on_demand_keeps_every_tool_reachable(monkeypatch):
    cmd = _leg(monkeypatch, on=True)
    assert "--tools" not in cmd
    assert _flag(cmd, "--plugin-dir") == str(claude_cli.ON_DEMAND_CODE_PLUGIN)
    assert "--setting-sources" not in cmd, "a code leg keeps the repo's CLAUDE.md"
    # A fresh building leg runs on Asta's coder prompt (claude_cli.CODER_PROMPT);
    # planning and resumed legs keep Claude Code's — see test_rules_once_per_session.
    assert _flag(cmd, "--system-prompt") == claude_cli.CODER_PROMPT


def test_a_code_leg_off_or_on_an_old_cli_is_unchanged(monkeypatch):
    assert "--tools" in _leg(monkeypatch, on=False)
    assert "--plugin-dir" not in _leg(monkeypatch, on=True, version=(2, 1, 267))


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_code_plugin_keeps_the_build_tools_in_front(tmp_path):
    mod = tmp_path / "register.mjs"
    shutil.copy(claude_cli.ON_DEMAND_CODE_PLUGIN / "hooks" / "register.js", mod)
    js = textwrap.dedent(f"""
        import {{ register }} from {json.dumps(str(mod))};
        let hook;
        register((event, h) => {{ if (event === "tool.describe") hook = h; }});
        const out = {{}};
        for (const tool of ["Bash", "Read", "Edit", "Write", "ToolSearch", "WebFetch", "Task", "CronCreate"]) {{
          const r = hook(null, {{ tool, description: "d" }}, (x) => ({{ ...x, passed: true }}));
          out[tool] = r.isDeferred === true ? "deferred" : "front";
        }}
        console.log(JSON.stringify(out));
    """)
    out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"Bash": "front", "Read": "front", "Edit": "front",
                                      "Write": "front", "ToolSearch": "front", "WebFetch": "deferred",
                                      "Task": "deferred", "CronCreate": "deferred"}


def test_the_delegate_rule_is_always_in_front(conv, monkeypatch):
    """16 Nov, live: without it a chat brain tried to write the code itself."""
    monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    system = _flag(claude_cli._build_cmd(conv, "add subtract to calc.py"), "--system-prompt")
    assert "delegate_task with kind 'code'" in system
    assert "You never edit, write or commit files yourself" in system


# --- no-tools calls (17 Nov) --------------------------------------------------

def test_a_no_tools_call_carries_only_asta_s_short_prompt(monkeypatch):
    """A Teams read was ~10k, ~7k of it Claude Code's prompt and his CLAUDE.md
    files — for a call that only reads the text it is handed."""
    import asyncio
    seen = {}

    async def _exec(*cmd, **_k):
        seen["cmd"] = list(cmd)
        raise RuntimeError("captured")

    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(RuntimeError, match="captured"):
        asyncio.run(claude_cli._one_shot("read this", None, 60, "", "", "", False, None,
                                         "", False, "haiku", True))
    cmd = seen["cmd"]
    assert _flag(cmd, "--tools") == ""
    assert _flag(cmd, "--system-prompt") == claude_cli.READER_PROMPT
    assert _flag(cmd, "--setting-sources") == ""
    assert "--strict-mcp-config" in cmd
