"""One file of his standing instructions, applied on every path — and only once.

"create one guardrails or some info file where i can add some common
instructions for coding and other things", after a week of "if i gave some
instructions it is not following". The rule he gave lived wherever the code
that heard it put it — a persona paragraph, a pipeline override, a memory fact
— so it held on one path and not the next. Now it lives in guardrails.md and is
routed by section to the prompts it belongs in.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import agent, copilot_cli, guardrails, store, tasks

ROOT = Path(__file__).resolve().parent.parent

RULES = """# mine
This preamble explains the file and is never sent.

## Coding
- Small, named, single-purpose functions.

## communication
- About 120 words on the phone.

## Investigation
- The prod namespace is `telikosprod`; runtime config is `helm/prod-values.yml`.

## Never
- Push without being told to ship.

## Weekend Mode
- Reply only to Vinish.
"""


@pytest.fixture
def rules(tmp_path, monkeypatch):
    p = tmp_path / "guardrails.md"
    p.write_text(RULES)
    monkeypatch.setenv("ASTA_GUARDRAILS", str(p))
    guardrails._cache.clear()
    yield p
    guardrails._cache.clear()


# --- the file -----------------------------------------------------------------

def test_sections_are_case_insensitive_and_the_preamble_is_not_sent(rules):
    secs = guardrails.sections()
    assert set(secs) == {"coding", "communication", "investigation", "never", "weekend mode"}
    assert "preamble" not in guardrails.block("chat")
    assert guardrails.section("COMMUNICATION") == "- About 120 words on the phone."


def test_each_section_goes_where_it_belongs(rules):
    code = guardrails.block("code")
    assert "Small, named" in code and "Push without" in code
    assert "120 words" not in code and "telikosprod" not in code
    analysis = guardrails.block("analysis")
    assert "telikosprod" in analysis and "Small, named" not in analysis
    draft = guardrails.block("draft")
    assert "120 words" in draft and "Small, named" not in draft


def test_a_section_he_invents_reaches_chat_where_he_can_see_it_work(rules):
    assert "Reply only to Vinish" in guardrails.block("chat")
    assert "Reply only to Vinish" not in guardrails.block("code")


def test_the_block_says_it_is_his_and_that_it_wins(rules):
    head = guardrails.block("code").splitlines()[0]
    assert "guardrails.md" in head and "override" in head


def test_edits_apply_without_a_restart(rules):
    assert "Weekend Mode" in guardrails.block("chat")
    rules.write_text(RULES.replace("## Weekend Mode", "## Holiday Mode"))
    import os
    os.utime(rules, (rules.stat().st_atime, rules.stat().st_mtime + 5))
    assert "Holiday Mode" in guardrails.block("chat")


def test_nothing_written_means_nothing_sent(tmp_path, monkeypatch):
    p = tmp_path / "g.md"
    p.write_text("# just a title\n\nno sections\n")
    monkeypatch.setenv("ASTA_GUARDRAILS", str(p))
    guardrails._cache.clear()
    assert guardrails.block("chat") == "" and guardrails.block("code") == ""
    assert "no '## Section' headings" in guardrails.problems()["guardrails"]


# --- no token bleed -----------------------------------------------------------

def test_a_long_section_is_cut_loudly_not_silently(tmp_path, monkeypatch):
    p = tmp_path / "g.md"
    p.write_text("## Coding\n" + "\n".join(f"- rule {i} " + "x" * 60 for i in range(60)))
    monkeypatch.setenv("ASTA_GUARDRAILS", str(p))
    guardrails._cache.clear()
    sent = guardrails.section("coding")
    assert len(sent) <= guardrails.SECTION_MAX + 120
    assert "cut here" in sent
    assert "over 1500 chars" in guardrails.problems()["guardrails"]
    assert "coding" in guardrails.problems()["guardrails"]


def test_a_missing_override_path_is_a_health_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTA_GUARDRAILS", str(tmp_path / "nowhere.md"))
    guardrails._cache.clear()
    assert "does not exist" in guardrails.problems()["guardrails"]
    assert guardrails.block("chat") == ""


def test_health_reports_guardrail_problems():
    assert "guardrails.problems()" in (ROOT / "app" / "health.py").read_text()


# --- the shipped default -----------------------------------------------------

def test_without_his_file_the_example_applies(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTA_GUARDRAILS", raising=False)
    monkeypatch.setattr(guardrails, "DEFAULT_PATH", tmp_path / "absent.md")
    assert guardrails.path() == guardrails.EXAMPLE_PATH


def test_the_example_is_tracked_and_his_file_is_not():
    assert guardrails.EXAMPLE_PATH.exists()
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "guardrails.md" in ignored


@pytest.mark.parametrize("rule", [
    "Small, named, single-purpose functions",
    "functional shape",
    "SIMPLIFY",
    "Guard clauses over nesting",
    "Comments explain WHY",
    "Delete what you replace",
    "Tests are part of the change",
])
def test_the_quality_bar_ships_in_the_example(rule):
    """The block that used to be hard-coded in CODE_OVERRIDES, now his to edit."""
    assert rule in guardrails.parse(guardrails.EXAMPLE_PATH.read_text())["coding"]


def test_the_example_has_every_routed_section():
    secs = guardrails.parse(guardrails.EXAMPLE_PATH.read_text())
    for name in ("always", "never", "coding", "communication", "investigation"):
        assert name in secs, name
    # Every shipped section has a declared audience — none of them is relying
    # on the "unknown section goes to chat" fallback by accident.
    assert set(secs) <= set(guardrails.AUDIENCES)


# --- every brain, every path --------------------------------------------------

def test_the_in_process_brain_gets_the_chat_block(rules):
    text = agent.build_instructions("", "", None)
    assert "120 words" in text and "telikosprod" in text
    assert "Small, named" not in text            # coding rules are for code legs


def test_the_cli_brains_get_the_same_block(rules):
    text = copilot_cli._first_turn_context({"id": "c-guard"}, via="Claude Code CLI")
    assert "120 words" in text and "Reply only to Vinish" in text


def test_the_persona_points_at_the_file_for_workspace_facts():
    """The prod namespace and the Helm path were hard-coded in the persona — a
    fact about HIS systems inside a public repo, and a second place to edit."""
    assert "telikosprod" not in agent.PERSONA
    assert "guardrails (Investigation)" in agent.PERSONA


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Run a task leg on a fake claude and keep the prompt it was handed."""
    seen: list[str] = []

    async def one_shot(prompt, **kw):
        seen.append(prompt)
        return "done"

    monkeypatch.setattr(tasks.claude_cli, "one_shot", one_shot)
    monkeypatch.setattr(tasks, "_resolve_executor", lambda tid: "claude")
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "_cwd", lambda ws: str(tmp_path))
    return seen


def test_a_fresh_code_leg_carries_the_coding_rules_once(rules, captured, tmp_path):
    t = store.create_task("x", "code", "do it", None)
    asyncio.run(tasks._run_code_leg(t["id"], "do it", str(tmp_path),
                                    resume=False, effort="medium"))
    assert captured[0].count("Small, named") == 1
    assert "120 words" not in captured[0]


def test_a_resumed_leg_does_not_re_send_them(rules, captured, tmp_path):
    """The session already holds them; sending them again is the bleed."""
    t = store.create_task("x", "code", "do it", None)
    store.kv_set(f"task_session:{t['id']}:claude", "sid-1")
    asyncio.run(tasks._run_code_leg(t["id"], "PLAN APPROVED", str(tmp_path),
                                    resume=True, effort="high"))
    assert "Small, named" not in captured[0]


def test_an_analysis_gets_the_investigation_rules(rules, captured):
    t = store.create_task("why is STF not done", "analysis", "look", None)
    asyncio.run(tasks._run_simple(t["id"], t, "look"))
    assert "telikosprod" in captured[0]
    assert "Small, named" not in captured[0]


def test_a_draft_gets_the_communication_rules(rules, captured):
    t = store.create_task("reply", "teams_draft", "draft it", None, "Someone")
    asyncio.run(tasks._run_simple(t["id"], t, "draft it"))
    assert "120 words" in captured[0]
    assert "telikosprod" not in captured[0]
