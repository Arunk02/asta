"""The capability registry is the single source of truth — prove it stays single."""

from __future__ import annotations

import inspect

import pytest

from app import capabilities


def test_every_row_resolves_to_a_real_function():
    for name, cap in capabilities.registry().items():
        assert callable(cap.fn), name


def test_every_capability_has_a_description():
    """The docstring IS the description. An empty one would ship a nameless tool
    to chat, the CLI brains and MCP at once."""
    for name, cap in capabilities.registry().items():
        assert len(cap.summary) > 20, f"{name} has no usable docstring"


def test_a_missing_function_is_a_hard_error():
    """Silently skipping would teach a tool that cannot be called."""
    bogus = capabilities.Capability("no_such_capability_anywhere", "test")
    original = capabilities._TABLE
    capabilities._TABLE = original + (bogus,)
    capabilities._REGISTRY = None
    try:
        with pytest.raises(RuntimeError, match="no_such_capability_anywhere"):
            capabilities.registry()
    finally:
        capabilities._TABLE = original
        capabilities._REGISTRY = None


def test_always_set_exists_and_is_small():
    reg = capabilities.registry()
    for name in capabilities.ALWAYS:
        assert name in reg, name
    assert len(capabilities.ALWAYS) < len(reg) / 3, "the always-set is meant to be tiny"


def test_tools_for_none_is_everything():
    assert len(capabilities.tools_for(None)) == len(capabilities.registry())


def test_tools_for_always_adds_the_core_set():
    tools = capabilities.tools_for(["jira_issue"])
    names = {t.__name__ for t in tools}
    assert "jira_issue" in names
    assert set(capabilities.ALWAYS) <= names


def test_selection_never_drops_a_hard_rule():
    """A tool exposed without its rule is the failure this design exists to stop."""
    notes = capabilities.notes_block(["teams_send_message"])
    assert "ONE-TO-ONE" in notes
    assert "jira" not in notes.lower(), "unrelated rules should not ride along"


def test_write_capabilities_carry_a_rule_or_are_obviously_safe():
    for name, cap in capabilities.registry().items():
        if cap.write and name != "reject_task":
            assert cap.note, f"{name} changes the outside world with no stated rule"


def test_cli_block_is_generated_not_written():
    """The point of the registry: adding a row teaches the CLI brains too."""
    block = capabilities.cli_block("9999", "/tmp/asta")
    assert "9999" in block
    for cap in capabilities.registry().values():
        if cap.http:
            assert cap.name in block, cap.name
        if cap.shell:
            assert cap.name in block, cap.name
    assert "ONE-TO-ONE" in block, "hard rules must reach the CLI brains too"


def test_cli_block_covers_every_http_and_shell_row():
    reg = capabilities.registry()
    reachable = [c for c in reg.values() if c.http or c.shell]
    assert len(reachable) > 20, "most capabilities should be reachable from a CLI brain"


def test_chat_agent_builds_from_the_registry():
    from app import agent
    a = agent.build_agent(["jira_issue"])
    assert a is not None
    # …and the narrowed instruction set carries the matching rules.
    text = agent.build_instructions("", "", None, "web", ["jira_comment"])
    assert "confirmation first" in text


def test_async_capabilities_are_declared_correctly():
    """A coroutine registered as a sync tool returns a coroutine object to the
    model — it looks like success and delivers nothing."""
    for name, cap in capabilities.registry().items():
        if inspect.iscoroutinefunction(cap.fn):
            assert cap.fn.__name__ == name
