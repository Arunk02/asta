"""The query rules exist. Making sure the workers that need them can see them.

Arun had already written the discipline into `skills/grafana-analyser.md`, and
the HARD RULE that loads it lives in the CHAT persona — which a spawned worker
never sees. So every rule was invisible to exactly the runs that needed it.

Task #96 proved it: asked why Send-To-Finance never ran for a booking, it queried
ONE service instead of the namespace, leaned on a code trace for its conclusion,
and left "is the country in disable-countries-to-billing?" open as out of scope —
when the answer was one grep of a prod-values.yml sitting in its own worktree.
Unset there means the `${VAR:default}` in application.yml applies: `[]`, nothing
disabled, theory ruled out in thirty seconds.
"""

from __future__ import annotations

import pathlib

import pytest

from app import responder

SKILL = pathlib.Path(__file__).resolve().parent.parent / "skills" / "grafana-analyser.md"

#: skills/*.md are machine-local (gitignored symlinks into his repos — see
#: .gitignore), so a CI runner has none. The tests that read the skill are
#: about ITS wording and skip where it does not exist; the tests that read
#: the persona and the briefs run everywhere.
needs_skill = pytest.mark.skipif(not SKILL.exists(), reason="skills/*.md are machine-local")


# --- the skill knows about production ----------------------------------------

@needs_skill
def test_the_skill_names_the_prod_namespace():
    """The env map was nonprod only. A skill that cannot name production cannot
    investigate the production question a colleague actually asked."""
    text = SKILL.read_text()
    assert "telikosprod" in text
    assert "no hyphen" in text, "it does not follow the nonprod pattern; say so"


@needs_skill
def test_it_says_production_is_the_default():
    assert "unless an env is named" in SKILL.read_text().lower()


@needs_skill
def test_it_says_one_namespace_covers_every_service():
    """"the answer to 'did it send?' is usually in a DIFFERENT service from the
    one the question names"."""
    text = SKILL.read_text().lower()
    assert "never query one service" in text


@needs_skill
def test_it_points_at_the_prod_helm_values():
    text = SKILL.read_text()
    assert "helm/prod-values.yml" in text
    assert "application.yml" in text, "an absent key means the default applies"


@needs_skill
def test_it_says_the_logs_decide_and_the_code_explains():
    text = SKILL.read_text()
    assert "The logs decide; the code explains." in text
    assert "hypothesis" in text


@needs_skill
def test_it_says_one_fetch_then_reason():
    """"since context already stays there" — re-querying for a detail already in
    the response costs a round trip and buys nothing."""
    text = SKILL.read_text()
    assert "One fetch, then think" in text


# --- and the workers are actually told ---------------------------------------

@pytest.mark.parametrize("kind", ["incident", "pr_review", "debug", "ask"])
def test_every_brief_carries_the_discipline(kind):
    """A rule only the chat persona states is a rule background work never gets."""
    brief = responder.brief_for(kind, "Vinish Kumar", "why is STF not done?")
    assert "load_skill('grafana-analyser')" in brief
    # The prod namespace and the Helm path are HIS facts, so they live in his
    # guardrails (Investigation) and reach the worker with the leg — see
    # test_guardrails.test_an_analysis_gets_the_investigation_rules. The brief
    # points there rather than carrying a second copy.
    assert "guardrails" in brief


def test_the_brief_survives_formatting_with_a_message_containing_braces():
    """`_CLOSING` is `.format`ted, so a literal `${VAR:default}` in it must be
    escaped or every brief raises instead of being written."""
    brief = responder.brief_for("debug", "Vinish", "check {this} and ${that}")
    assert "${VAR:default}" in brief


def test_the_worker_is_told_the_logs_come_first():
    brief = responder.brief_for("debug", "Vinish Kumar", "why is STF not done?")
    assert "logs decide" in brief and "hypothesis" in brief


def test_staging_the_reply_is_still_the_last_word():
    """The discipline is prepended, so the send rule must not have been pushed
    out of the brief."""
    brief = responder.brief_for("debug", "Vinish Kumar", "why?")
    assert "prepare_to_send — never send anything yourself" in brief


# --- the fact that started it ------------------------------------------------

@needs_skill
def test_an_absent_helm_key_is_an_answer_not_an_unknown():
    """Pinned as the worked example, because the shape recurs: a config-based
    theory left open is a half-answer sent to a colleague who asked a direct
    question."""
    text = SKILL.read_text()
    assert "LIST_OF_DISABLE_COUNTRIES_TO_BILLING" in text
    assert "not in prod-values.yml" in text


# --- and the same rule for every MCP server, not just grafana ----------------

def test_the_one_fetch_rule_covers_every_mcp_server():
    """"avoid doing multiple mcp calls, get all the logs details at once and use
    that through the analysis, since context already stays there" — said about
    MCP generally, so it cannot live only in the grafana skill."""
    from app import agent
    p = agent.PERSONA
    assert "EVERY MCP server, not just grafana" in p
    assert "ONE wide call" in p
    assert "guardrails (Investigation)" in p      # the facts themselves are his file's


def test_the_chat_brain_is_told_logs_decide_too():
    from app import agent
    assert "LOGS decide" in agent.PERSONA and "hypothesis" in agent.PERSONA


def test_the_persona_still_formats():
    """`PERSONA` is `.format`ted for {name}, so a literal `${VAR:default}` in it
    reads as a field and raises KeyError on EVERY turn — the whole assistant,
    not one investigation. Caught by three unrelated tests at once."""
    from app import agent
    rendered = agent.PERSONA.format(name="Asta")
    assert "You are Asta" in rendered


def test_a_brace_in_his_guardrails_never_meets_format(tmp_path, monkeypatch):
    """The `${VAR:default}` fact moved into guardrails.md, which he edits freely
    and which is NOT a format string. It is appended AFTER the persona is
    formatted, so a brace of his can never raise the KeyError above."""
    from app import agent, guardrails
    p = tmp_path / "g.md"
    p.write_text("## Investigation\n- absent key means `${VAR:default}` applies\n")
    monkeypatch.setenv("ASTA_GUARDRAILS", str(p))
    guardrails._cache.clear()
    text = agent.build_instructions("", "", None)
    assert "`${VAR:default}` applies" in text
    guardrails._cache.clear()


def test_a_read_only_pass_does_not_stop_at_the_service_boundary():
    """Run #97 improved hugely but still scoped to one container and said "I
    didn't have logs from the billing service in scope" — then stopped one query
    short of the answer. Reading logs changes nothing, so scope is not a reason
    to stop; and the evidence for "did it land?" sits downstream by definition."""
    brief = responder.brief_for("debug", "Vinish Kumar", "why is STF not done?")
    assert "EVERY service in that namespace is in scope" in brief
    assert "DOWNSTREAM" in brief
