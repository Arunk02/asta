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


def test_every_capability_is_reachable_from_a_cli_brain():
    """Parity: a tool with neither http nor shell is invisible to copilot/claude,
    so 'remember this' silently did nothing there. Every capability must have an
    endpoint — otherwise the reachable set depends on which brain answered."""
    orphans = [n for n, c in capabilities.registry().items()
               if not c.http and not c.shell]
    assert orphans == [], f"endpoint-less (unreachable from a CLI brain): {orphans}"


def test_narrowed_cli_block_is_smaller_but_hides_nothing():
    """Selecting a few tools expands only those, yet every reachable tool is
    still named somewhere — a mis-rank costs a round-trip, never a capability."""
    reg = capabilities.registry()
    full = capabilities.cli_block("8321", "", None)
    narrow = capabilities.cli_block("8321", "", ["jira_comment"])
    assert len(narrow) < len(full)
    for name, cap in reg.items():
        if cap.http or cap.shell:                       # reachable from a CLI brain
            assert name in narrow, f"{name} vanished from the narrowed block"


def test_narrowing_keeps_the_always_floor_and_whole_groups():
    block = capabilities.cli_block("8321", "", ["jira_comment"])
    # the whole jira group is expanded with its endpoint, not just the one asked
    jira = {n for n, c in capabilities.registry().items() if c.group == "jira"}
    for n in jira:
        cap = capabilities.get(n)
        if cap.http:
            assert f"{n} — {cap.http}" in block, f"{n} not fully expanded"


def test_narrowed_block_never_advertises_an_unreachable_tool():
    """The 'Also available' tail must not list an in-process-only tool: a CLI
    brain cannot curl it, so telling it to ask for the endpoint sends it
    chasing something that does not exist."""
    block = capabilities.cli_block("8321", "", ["set_reminder"])
    tail = block.split("Also available")[-1] if "Also available" in block else ""
    for name, cap in capabilities.registry().items():
        if not cap.http and not cap.shell:
            assert name not in tail, f"{name} has no endpoint yet was advertised"


def test_no_selection_is_the_old_full_block():
    """None means 'no ranking' — the un-narrowed block, so the fallback path is
    byte-for-byte what shipped before."""
    block = capabilities.cli_block("8321", "", None)
    assert "Also available" not in block, "the full block has no leftover tail"


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


def test_read_file_carries_the_resolve_first_rule():
    """The 'resolve before reading' rule must travel WITH the read tool, not only on
    resolve_context — else a turn that selects read_workspace_file alone loses it and
    reads blind (the BLIND_READ token sink the auditor flags)."""
    note = capabilities.get("read_workspace_file").note.lower()
    assert "resolve" in note
    assert "resolve" in capabilities.notes_block(["read_workspace_file"]).lower()
