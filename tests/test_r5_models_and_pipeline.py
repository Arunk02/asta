"""Round 5, PR F: current model ids, Code Mode kept out of headless legs, and the
pipeline sized by the same tier routing uses."""

from __future__ import annotations

from app import agent, claude_cli, pi_cli, routing, store, tasks


def test_code_mode_is_off_for_every_asta_leg(monkeypatch):
    # Turned on in his user settings for interactive use, it must not reach a
    # headless leg; "0" anywhere keeps it off.
    monkeypatch.setenv("CLAUDE_CODE_EXPERIMENTAL_CODE_MODE", "1")
    assert claude_cli._subprocess_env()["CLAUDE_CODE_EXPERIMENTAL_CODE_MODE"] == "0"


def test_default_claude_api_model_is_sonnet_5_5(monkeypatch):
    assert agent.spec("claude")["model_env"][1] == "claude-sonnet-5-5"


def test_pi_aliases_map_to_the_5_5_family():
    assert pi_cli.model_id("opus") == "anthropic/claude-opus-5-5"
    assert pi_cli.model_id("sonnet") == "anthropic/claude-sonnet-5-5"
    assert pi_cli.model_id("haiku") == "anthropic/claude-haiku-5-5"
    assert pi_cli.DEFAULT_MODEL == "anthropic/claude-sonnet-5-5"


def _tier(task_id: int, tier: int) -> None:
    routing.set_tier(task_id, tier, "test", "signals")


def test_t1_ticket_runs_micro(monkeypatch):
    monkeypatch.setenv("ASTA_ROUTING", "1")
    _tier(9101, routing.T1)
    assert tasks._pipeline_by_tier(9101, "ABC-12 set a config value", "") == "micro"


def test_t2_and_t3_ad_hoc_run_the_staged_pipeline(monkeypatch):
    monkeypatch.setenv("ASTA_ROUTING", "1")
    _tier(9102, routing.T2)
    _tier(9103, routing.T3)
    assert tasks._pipeline_by_tier(9102, "add a field", "") == "full"
    assert tasks._pipeline_by_tier(9103, "change the workflow", "") == "full"


def test_without_routing_the_ticket_rule_stands(monkeypatch):
    monkeypatch.setenv("ASTA_ROUTING", "0")
    assert tasks._pipeline_by_tier(9104, "ABC-12 thing", "") == "full"
    assert tasks._pipeline_by_tier(9105, "small thing", "") == "micro"
    assert store.kv_get(routing._key(9104)) in (None, "")
