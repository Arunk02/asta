"""One engine, not two.

Missions and tasks both ran plan → approve → implement → verify → ship. Two entry
points to one concept meant two places to fix every bug, and the agent had to
choose between them on every request. These tests are the fence that keeps the
second engine from growing back.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app import agent, capabilities, repo_ops

ROOT = Path(__file__).resolve().parent.parent


def test_the_second_engine_is_gone():
    assert not (ROOT / "app" / "missions.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.missions")


def test_nothing_imports_it():
    for path in (ROOT / "app").glob("*.py"):
        text = path.read_text()
        assert "from .missions" not in text, path.name
        assert "import missions" not in text, path.name


def test_the_agent_offers_exactly_one_way_to_start_work():
    """Two tools for one concept is the thing that made the agent choose wrongly."""
    names = set(capabilities.names())
    assert "delegate_task" in names
    assert not {n for n in names if "mission" in n}


def test_shared_helpers_live_outside_both_engines():
    """These belong to neither engine; tasks importing them out of missions is
    what kept the dead module alive."""
    assert repo_ops.playbook_block(ROOT) == "" or "playbooks" in repo_ops.playbook_block(ROOT)
    assert "Co-Authored-By" in repo_ops.NO_ATTRIBUTION
    assert repo_ops.BASE_BRANCHES == ("main", "master", "develop")


@pytest.mark.parametrize("jira,title,tid,expected", [
    ("ABC-1", "anything", 7, "feature/ABC-1"),
    ("", "Fix the thing!", 7, "feature/asta-7-fix-the-thing"),
])
def test_branch_naming(jira, title, tid, expected):
    assert repo_ops.branch_name(jira, title, tid) == expected


def test_no_attribution_rule_survived_the_move():
    """Arun's commits must read as his own work — the rule that must not be lost."""
    text = repo_ops.NO_ATTRIBUTION.lower()
    assert "never add a co-authored-by" in text
    assert "generated with" in text


def test_ship_is_a_capability_not_a_dead_end():
    """tasks.ship existed but no tool reached it — the flow said 'ship_mission',
    which no longer existed."""
    assert "ship_task" in capabilities.names()
    assert callable(agent.ship_task)


def test_persona_points_at_the_surviving_engine():
    import re
    text = agent.PERSONA.lower()
    assert not re.search(r"\bmissions?\b", text)   # \b, or "permission" matches
    assert "background task" in text
