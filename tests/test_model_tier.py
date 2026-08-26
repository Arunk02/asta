"""Which MODEL answers, as opposed to which brain runs it.

Two different choices, and only the first was ever switchable. The tier lived in
ASTA_CLAUDE_CLI_MODEL, read from the environment at call time — fixed until a
restart, absent from the UI, and unreachable from WhatsApp, which is where Arun
actually decides "this one needs opus".

The design constraint these tests hold is that the tier stays DATA on the brain
spec, so a brain that grows tiers becomes switchable without touching the
switching code — the same contract as identity / tools / context.
"""

from __future__ import annotations

import pytest

from app import agent, claude_cli, main, store

#: Captured at import, before conftest's autouse guard replaces `one_shot` with a
#: refusal. That guard is right — no test may spawn a real CLI brain — and this
#: does not weaken it: the subprocess is stubbed below, so what runs is the
#: argv-building, which is the only part under test here.
_REAL_ONE_SHOT = claude_cli.one_shot


@pytest.fixture(autouse=True)
def _conv():
    store.create_conversation("c1", "copilot")
    return {"id": "c1", "model": "copilot"}


# --- the setting itself --------------------------------------------------------

def test_with_nothing_set_there_is_no_tier():
    """The CLI picks its own default; Asta does not invent one."""
    assert agent.tier_of("claude_cli") == ""


def test_the_environment_is_the_fallback(monkeypatch):
    """An unattended box with no stored choice behaves exactly as before."""
    monkeypatch.setenv("ASTA_CLAUDE_CLI_MODEL", "claude-sonnet-5")
    assert agent.tier_of("claude_cli") == "claude-sonnet-5"


def test_his_choice_beats_the_environment(monkeypatch):
    monkeypatch.setenv("ASTA_CLAUDE_CLI_MODEL", "claude-sonnet-5")
    agent.set_tier("claude_cli", "opus")
    assert agent.tier_of("claude_cli") == "opus"


def test_clearing_hands_the_decision_back(monkeypatch):
    monkeypatch.setenv("ASTA_CLAUDE_CLI_MODEL", "claude-sonnet-5")
    agent.set_tier("claude_cli", "opus")
    agent.set_tier("claude_cli", "")
    assert agent.tier_of("claude_cli") == "claude-sonnet-5"


def test_a_brain_with_no_tiers_offers_none():
    assert agent.tiers_for("copilot") == ()
    assert agent.tier_of("copilot") == ""


def test_the_live_pin_is_offered_even_when_it_is_not_an_alias(monkeypatch):
    """`.env` pins a full model id, which is a legitimate choice. Offering only
    the short aliases would show a picker whose current value is missing from its
    own list, and reject the setting the machine is actually running."""
    monkeypatch.setenv("ASTA_CLAUDE_CLI_MODEL", "claude-sonnet-5")
    assert "claude-sonnet-5" in agent.tier_options("claude_cli")
    assert "opus" in agent.tier_options("claude_cli")


def test_a_declared_alias_is_not_duplicated():
    agent.set_tier("claude_cli", "opus")
    assert agent.tier_options("claude_cli").count("opus") == 1


@pytest.mark.parametrize("tier,brain", [("opus", "claude_cli"), ("sonnet", "claude_cli"),
                                        ("frobnicator", "")])
def test_a_tier_names_the_brain_that_offers_it(tier, brain):
    assert agent.brain_for_tier(tier) == brain


# --- saying it from any channel ------------------------------------------------

def test_use_opus_switches_the_model_and_the_brain(_conv):
    """He names the model he wants, not the runner it lives behind."""
    reply = main._switch_model(_conv, main._model_request("use opus"))
    assert "opus" in reply
    assert agent.tier_of("claude_cli") == "opus"
    assert _conv["model"] == "claude_cli"


def test_switching_tier_on_the_brain_he_is_already_using_says_nothing_extra(_conv):
    main._switch_model(_conv, "opus")
    reply = main._switch_model(_conv, "sonnet")
    assert agent.tier_of("claude_cli") == "sonnet"
    assert "This chat is now on" not in reply


def test_the_tier_applies_beyond_this_chat(_conv):
    """Deliberately global: a code task delegated from here runs in its own lane,
    and "this needs opus" is a statement about the work, not about the window."""
    main._switch_model(_conv, "opus")
    assert agent.tier_of("claude_cli") == "opus"        # no conversation in sight


def test_an_unknown_name_still_lists_the_real_ones(_conv):
    reply = main._switch_model(_conv, "frobnicator")
    assert "don't have a brain called" in reply and "claude_cli" in reply


def test_switching_brains_still_works(_conv):
    """The tier must not have eaten the older, more common command."""
    assert "Now using" in main._switch_model(_conv, "claude_cli")
    assert _conv["model"] == "claude_cli"


def test_the_listing_tells_him_the_models_exist(_conv):
    listing = main._model_listing("copilot")
    assert "opus" in listing and "sonnet" in listing


def test_the_picker_shows_which_model_is_answering():
    agent.set_tier("claude_cli", "opus")
    info = agent.model_registry()["claude_cli"]
    assert info["label"].endswith("· opus")
    assert info["tier"] == "opus"


# --- and it reaches the process ------------------------------------------------

def test_the_choice_becomes_the_model_argument():
    """The point of the whole thing: a setting nothing passes to the CLI is a
    setting that does not exist."""
    agent.set_tier("claude_cli", "opus")
    cmd = claude_cli._build_cmd({"id": "c1", "workspace": ""}, "hello")
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "opus"


def test_no_choice_means_no_argument():
    cmd = claude_cli._build_cmd({"id": "c1", "workspace": ""}, "hello")
    assert "--model" not in cmd


class _Captured(Exception):
    """A sentinel distinct from RuntimeError. `one_shot` raises RuntimeError when
    the CLI is missing, so catching that would let an unrelated early failure
    look exactly like a pass — which is how the first version of this test
    reported success while capturing nothing at all."""


def _one_shot_cmd(monkeypatch) -> list[str]:
    """The argv `one_shot` would have executed."""
    import asyncio
    seen: dict = {}

    async def _exec(*cmd, **_kw):
        seen["cmd"] = list(cmd)
        raise _Captured()

    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(_Captured):
        asyncio.run(_REAL_ONE_SHOT("implement it"))
    return seen["cmd"]


def test_a_task_run_gets_the_tier_too(monkeypatch):
    """The path that matters most, and the one a first pass left uncovered.

    Chat is where he SAYS "use opus"; a delegated code task is where opus
    actually earns its keep. `one_shot` builds its command separately from
    `_build_cmd`, so a fix applied to one and not the other would look right in
    chat and quietly run sonnet on every implementation. Mutation testing found
    exactly that gap.
    """
    agent.set_tier("claude_cli", "opus")
    cmd = _one_shot_cmd(monkeypatch)
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "opus"


def test_a_task_run_with_no_choice_passes_no_model(monkeypatch):
    assert "--model" not in _one_shot_cmd(monkeypatch)


def test_the_env_pin_stays_selectable_after_switching_away(monkeypatch):
    """A live run caught this: after picking `sonnet`, the pinned
    `claude-sonnet-5` dropped out of the options, so the endpoint refused it and
    going back to the machine's own default became impossible from the UI. A
    one-way door nobody notices until they want to walk back through it."""
    monkeypatch.setenv("ASTA_CLAUDE_CLI_MODEL", "claude-sonnet-5")
    agent.set_tier("claude_cli", "sonnet")
    assert "claude-sonnet-5" in agent.tier_options("claude_cli")


def test_the_options_have_no_duplicates(monkeypatch):
    monkeypatch.setenv("ASTA_CLAUDE_CLI_MODEL", "opus")
    agent.set_tier("claude_cli", "opus")
    opts = agent.tier_options("claude_cli")
    assert len(opts) == len(set(opts))
