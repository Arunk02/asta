"""The debugging discipline, and that every worker that needs it gets it.

Until 13 Oct the discipline lived in `skills/grafana-analyser.md`, a symlink into
the booking-service repo, and it taught the opposite of what solved the
12 Oct case: prod is `telikosprod` (it is telikos-prod), "unless an env is named,
the question is about prod" (Deepa's booking was in QA), namespace-only queries
(Loki refuses them for size), Prometheus for performance only (the lost log
lines were found there). It is replaced by Asta's own `skills/debugging.md`.

Task #96 is still the reason the briefs carry it: a worker never sees the chat
persona, so a rule only the persona states is a rule background work never gets.
"""

from __future__ import annotations

import pathlib

import pytest

from app import responder, roles

SKILL = pathlib.Path(__file__).resolve().parent.parent / "skills" / "debugging.md"


# --- the skill tells the truth about this estate ----------------------------

def test_the_skill_is_shipped_not_a_link_to_someone_elses_repo():
    assert SKILL.exists() and not SKILL.is_symlink()
    assert not (SKILL.parent / "grafana-analyser.md").exists()


def test_the_skill_names_the_real_namespaces():
    text = SKILL.read_text()
    assert "`telikos-prod`" in text and "telikosprod" not in text
    assert "| perf | `telikos-spt`" in text


def test_it_never_assumes_production():
    text = SKILL.read_text().lower()
    assert "never\nassume prod" in text or "never assume prod" in text
    assert "unless an env is named" not in text


def test_it_carries_the_method_with_the_tools_that_do_it():
    text = SKILL.read_text()
    for tool in ("log_counts", "log_lines", "find_id", "log_gaps", "temporal_workflow"):
        assert tool in text, tool
    assert "A missing line is a question, not an answer" in text


def test_it_keeps_the_rules_that_were_right():
    text = SKILL.read_text()
    assert "helm/prod-values.yml" in text and "application.yml" in text
    assert "The logs decide; the code explains." in text and "hypothesis" in text


# --- and the workers are actually told ---------------------------------------

@pytest.mark.parametrize("kind", ["incident", "pr_review", "debug", "ask"])
def test_every_brief_carries_the_discipline(kind):
    """A rule only the chat persona states is a rule background work never gets."""
    brief = responder.brief_for(kind, "Alex Kumar", "why is STF not done?")
    for tool in ("log_counts", "log_lines", "find_id", "log_gaps"):
        assert tool in brief, tool
    assert "never assume production" in brief
    assert "load_skill('grafana-analyser')" not in brief


def test_the_brief_survives_formatting_with_a_message_containing_braces():
    """`_CLOSING` is `.format`ted, so a literal `${VAR:default}` in it must be
    escaped or every brief raises instead of being written."""
    brief = responder.brief_for("debug", "Alex", "check {this} and ${that}")
    assert "${VAR:default}" in brief


def test_the_worker_is_told_the_logs_come_first():
    brief = responder.brief_for("debug", "Alex Kumar", "why is STF not done?")
    assert "logs decide" in brief and "hypothesis" in brief


def test_negative_findings_are_bounded_by_the_observed_window():
    brief = responder.brief_for("debug", "Alex Kumar", "was there a retry?")
    assert "actual start and end" in brief
    assert "AFTER a recent failure" in brief
    assert "grafana_logs(at=ISO-time-with-zone" in brief
    assert "2000-line limit" in brief


def test_staging_the_reply_is_still_the_last_word():
    """The discipline is prepended, so the send rule must not have been pushed
    out of the brief."""
    brief = responder.brief_for("debug", "Alex Kumar", "why?")
    assert "prepare_to_send — never send anything yourself" in brief


# --- and the same rule for every MCP server, not just grafana ----------------

def test_debugging_builds_each_query_on_the_last_without_refetching():
    """His 13 Oct ask — "how you are doing it" — took about fifteen small
    queries, each built on the one before. What stays from the old one-call rule
    is its intent: never re-fetch what you already have."""
    from app import agent
    p = agent.PERSONA
    assert "Never re-run a query whose" in p and "answer you already have" in p
    assert "ONE wide call" not in p and "grafana-analyser" not in p


def test_a_why_question_gets_the_case_method():
    assert "log_gaps" in roles.CASE_METHOD and "never guess one" in roles.CASE_METHOD
