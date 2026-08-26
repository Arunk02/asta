"""Per-brain differences are DECLARED TRAITS, not scattered `if model == …`.

The consistency principle: adding a model is a row in the spec table (identity,
tools, context, rank) plus its runner — never a new branch at each call site.
These pin the traits table as the single source of truth and check the code that
matters actually reads from it.
"""

from __future__ import annotations

from app import agent


def test_every_brain_declares_complete_traits():
    for name in agent._SPECS:
        t = agent.brain_traits(name)
        assert t["identity"] in ("system", "prefix"), name
        assert t["tools"] in ("mcp", "in_process", "none"), name
        assert t["context"] > 0, name
        assert isinstance(t["rank"], int), name


def test_fallback_order_is_subscription_then_local_then_api():
    # ranks: copilot 10, claude_cli 20, local 30, claude 40, openai 50 — cheapest
    # and strongest first, metered API keys last.
    assert agent.fallback_order() == ["copilot", "claude_cli", "local", "claude", "openai"]


def test_local_declares_the_small_context_window():
    # Local is the forcing function for lean tool budgets — its window is the one
    # the token/local work must fit inside.
    assert agent.brain_traits("local")["context"] < agent.brain_traits("claude_cli")["context"]


def test_both_cli_brains_reach_capabilities_over_mcp():
    assert agent.brain_traits("claude_cli")["tools"] == "mcp"
    assert agent.brain_traits("copilot")["tools"] == "mcp"


def test_identity_trait_matches_the_builder(monkeypatch):
    """Claude declares identity=system, and its command actually delivers the
    orientation as a system prompt; Copilot declares prefix (no system flag)."""
    from app import claude_cli
    assert agent.brain_traits("claude_cli")["identity"] == "system"
    assert agent.brain_traits("copilot")["identity"] == "prefix"
    cmd = claude_cli._build_cmd({"id": "trait-c"}, "hello")
    assert "--append-system-prompt" in cmd          # honours the declared trait


def test_an_unknown_brain_gets_safe_conservative_defaults():
    t = agent.brain_traits("some-future-model")
    assert t["tools"] == "in_process" and t["context"] == 8192 and t["rank"] == 999
