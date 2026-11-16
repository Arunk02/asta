"""The code rules ride once, when a leg's session starts (16 Nov).

Appended to the prompt, they went into every resumed leg again — 11k characters
per leg — and a leg that builds got the plan's phone format it never uses.
"""

import asyncio

import pytest

from app import store, tasks


@pytest.fixture
def full_task(monkeypatch):
    t = store.create_task("Map the equipment size", "code", "map it", workspace="booking")
    monkeypatch.setattr(tasks, "_agent_for", lambda _t: "solo")
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "context_boot_paths", lambda p, *a: p)
    store.kv_set(f"task_executor:{t['id']}", "claude")
    return t["id"]


def _leg(monkeypatch, tid, resume):
    seen = {}

    async def _cli(task_id, executor, prompt, **kw):
        seen["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(tasks, "_observed_cli", _cli)
    asyncio.run(tasks._code_leg(tid, "the ask", "/tmp", resume=resume, effort="high",
                                workspace="booking"))
    return seen["prompt"]


def test_a_planning_session_starts_with_every_rule(monkeypatch, full_task):
    p = _leg(monkeypatch, full_task, resume=False)
    assert p.count("THE PLAN MUST BE READABLE") == 1 and "Boot 0 efficiency" in p


def test_a_resumed_leg_is_not_sent_them_again(monkeypatch, full_task):
    _leg(monkeypatch, full_task, resume=False)
    p = _leg(monkeypatch, full_task, resume=True)
    assert "Boot 0 efficiency" not in p and "THE PLAN MUST BE READABLE" not in p


def test_a_fresh_building_session_gets_the_rules_without_the_plan_format(monkeypatch, full_task):
    tasks.mark_approved(full_task)
    p = _leg(monkeypatch, full_task, resume=False)
    assert "Boot 0 efficiency" in p and "ONE BRANCH, ONE WORKTREE" in p
    assert "THE PLAN MUST BE READABLE" not in p and "THE PLAN GATE IS UNCONDITIONAL" not in p


def test_a_lost_session_counts_as_fresh(monkeypatch, full_task):
    p = _leg(monkeypatch, full_task, resume=True)          # no session stored yet
    assert "Boot 0 efficiency" in p


def test_micro_carries_its_own(monkeypatch, full_task):
    store.kv_set(f"task_pipeline:{full_task}", "micro")
    assert "Boot 0 efficiency" not in _leg(monkeypatch, full_task, resume=False)


def test_the_build_rules_are_the_full_rules_minus_the_planning_ones():
    assert len(tasks.CODE_BUILD_RULES) < len(tasks.CODE_OVERRIDES) * 0.6
    for head in tasks._PLAN_ONLY_RULES:
        assert head in tasks.CODE_OVERRIDES and head not in tasks.CODE_BUILD_RULES
    for kept in ("Never run a command in the background", "Commits: plain", "Build-output hygiene"):
        assert kept in tasks.CODE_BUILD_RULES


def test_the_first_prompt_no_longer_carries_them(full_task):
    assert "Boot 0 efficiency" not in tasks.first_code_prompt(full_task, store.get_task(full_task))


# --- open work and PRs: on a session's first turn, then only on change ---------

def test_unchanged_open_work_is_not_resent_to_the_same_session(monkeypatch):
    from app import claude_cli, copilot_cli
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    monkeypatch.setattr(copilot_cli, "open_work", lambda: "OPEN-WORK-BLOCK v1")
    monkeypatch.setattr(copilot_cli, "_his_prs", lambda *a, **k: "PR-BLOCK v1")
    conv = store.create_conversation(model="claude_cli", workspace=None)
    msg = lambda cmd: cmd[cmd.index("-p") + 1]
    first = msg(claude_cli._build_cmd(conv, "status of task 7 please, and fix it"))
    assert "OPEN-WORK-BLOCK v1" in first and "PR-BLOCK v1" in first
    second = msg(claude_cli._build_cmd(conv, "and the other one — fix it too"))
    assert "OPEN-WORK-BLOCK" not in second and "PR-BLOCK" not in second
    monkeypatch.setattr(copilot_cli, "open_work", lambda: "OPEN-WORK-BLOCK v2")
    third = msg(claude_cli._build_cmd(conv, "anything new? fix it"))
    assert "OPEN-WORK-BLOCK v2" in third and "PR-BLOCK" not in third
    store.kv_set(f"claude_session:{conv['id']}", "")                 # session retired
    fresh = msg(claude_cli._build_cmd(conv, "go on, fix it"))
    assert "OPEN-WORK-BLOCK v2" in fresh and "PR-BLOCK v1" in fresh


def test_a_side_read_never_marks_a_block_as_sent(monkeypatch):
    from app import copilot_cli
    monkeypatch.setattr(copilot_cli, "open_work", lambda: "OPEN-WORK-BLOCK v1")
    cid = store.create_conversation(model="claude_cli", workspace=None)["id"]
    assert "OPEN-WORK-BLOCK" in copilot_cli.turn_context("hi", cid, fresh=True, remember=False)
    assert "OPEN-WORK-BLOCK" in copilot_cli.turn_context("hi", cid, fresh=False, remember=True)
