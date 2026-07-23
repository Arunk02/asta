"""Project-context generation: planning, ordering, and honest failure."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app import context_build as cb
from app import workspace as ws


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    cfg = tmp_path / "workspaces.json"
    cfg.write_text("{}")
    monkeypatch.setenv("ASTA_WORKSPACES_FILE", str(cfg))
    yield


def _repo(path: Path) -> Path:
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "src" / "a.txt").write_text("a")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    return path


@pytest.fixture
def two_repo_ws(tmp_path):
    root = tmp_path / "wsp"
    _repo(root / "alpha")
    _repo(root / "beta")
    return root


def _give_index(root: Path, repo: str, ctx: str = ".asta-context"):
    d = root / ctx / "repos" / repo
    d.mkdir(parents=True, exist_ok=True)
    (d / "_index.json").write_text(json.dumps({"repo": repo, "verified_against": "abc123"}))
    (d / "OVERVIEW.md").write_text(f"# {repo}\n")


# --- planning ----------------------------------------------------------------

def test_plan_lists_everything_when_nothing_built(two_repo_ws):
    ws.add("w", two_repo_ws)
    p = cb.plan("w")
    assert p["ok"] and p["needs_build"]
    assert set(p["repos_to_build"]) == {"alpha", "beta"}
    assert p["repos_already_done"] == []
    assert "token spend" in p["estimate"], "cost must be stated before spending"


def test_plan_skips_repos_that_already_have_an_index(two_repo_ws):
    ws.add("w", two_repo_ws)
    _give_index(two_repo_ws, "alpha")
    p = cb.plan("w")
    assert p["repos_to_build"] == ["beta"]
    assert p["repos_already_done"] == ["alpha"]


def test_plan_respects_the_selected_repo_subset(two_repo_ws):
    ws.add("w", two_repo_ws, repos=["alpha"])
    p = cb.plan("w")
    assert p["repos_to_build"] == ["alpha"], "beta was not selected"


def test_plan_reports_nothing_to_do(two_repo_ws):
    ws.add("w", two_repo_ws)
    _give_index(two_repo_ws, "alpha")
    _give_index(two_repo_ws, "beta")
    p = cb.plan("w")
    assert not p["needs_build"]
    assert "already has an index" in p["estimate"]


def test_plan_on_unknown_workspace(two_repo_ws):
    assert cb.plan("nope")["ok"] is False


def test_context_dir_follows_the_configured_layout(two_repo_ws, monkeypatch):
    (two_repo_ws / ".other-ctx").mkdir()
    monkeypatch.setenv("ASTA_CONTEXT_DIRNAME", ".other-ctx")
    ws.add("w", two_repo_ws)
    assert cb.context_dir("w").name == ".other-ctx", "never assume the directory name"


# --- building ----------------------------------------------------------------

def test_build_is_a_noop_when_everything_exists(two_repo_ws):
    ws.add("w", two_repo_ws)
    _give_index(two_repo_ws, "alpha")
    _give_index(two_repo_ws, "beta")
    out = asyncio.run(cb.build("w", notify_when_done=False))
    assert "already has context" in out


def test_build_runs_each_repo_then_the_generators_once(two_repo_ws, monkeypatch):
    """Ordering is the correctness property: the generators flatten every
    _index.json, so running them before all repos finish yields a partial index
    that still validates."""
    ws.add("w", two_repo_ws)
    order = []

    async def fake_one(workspace, repo, executor=""):
        order.append(("build", repo))
        _give_index(two_repo_ws, repo)
        return repo, True, "ok"

    async def fake_provision(name, repos=None):
        order.append(("provision", name))
        return "generators ran"

    monkeypatch.setattr(cb, "_build_one", fake_one)
    monkeypatch.setattr(cb.ws_mod, "provision", fake_provision)
    out = asyncio.run(cb.build("w", notify_when_done=False))

    assert [o[0] for o in order].count("provision") == 1, "generators run exactly once"
    assert order[-1][0] == "provision", "and only after every repo"
    assert {o[1] for o in order if o[0] == "build"} == {"alpha", "beta"}
    assert "✓ alpha" in out and "✓ beta" in out


def test_build_does_not_run_generators_when_every_repo_failed(two_repo_ws, monkeypatch):
    ws.add("w", two_repo_ws)
    ran = []

    async def fake_one(workspace, repo, executor=""):
        return repo, False, "executor exploded"

    async def fake_provision(name, repos=None):
        ran.append(name)
        return "should not happen"

    monkeypatch.setattr(cb, "_build_one", fake_one)
    monkeypatch.setattr(cb.ws_mod, "provision", fake_provision)
    out = asyncio.run(cb.build("w", notify_when_done=False))
    assert ran == [], "nothing to index, so no generator run"
    assert "✗ alpha" in out and "executor exploded" in out


def test_build_survives_one_repo_raising(two_repo_ws, monkeypatch):
    ws.add("w", two_repo_ws)

    async def fake_one(workspace, repo, executor=""):
        if repo == "alpha":
            raise RuntimeError("boom")
        _give_index(two_repo_ws, repo)
        return repo, True, "ok"

    monkeypatch.setattr(cb, "_build_one", fake_one)
    monkeypatch.setattr(cb.ws_mod, "provision", lambda n, repos=None: _done("gen"))
    out = asyncio.run(cb.build("w", notify_when_done=False))
    assert "✓ beta" in out, "one bad repo must not sink the batch"


async def _done(x):
    return x


def test_build_clears_its_in_progress_flag(two_repo_ws, monkeypatch):
    ws.add("w", two_repo_ws)

    async def fake_one(workspace, repo, executor=""):
        assert cb.in_progress("w"), "flag is set while running"
        _give_index(two_repo_ws, repo)
        return repo, True, "ok"

    monkeypatch.setattr(cb, "_build_one", fake_one)
    monkeypatch.setattr(cb.ws_mod, "provision", lambda n, repos=None: _done("gen"))
    asyncio.run(cb.build("w", notify_when_done=False))
    assert not cb.in_progress("w"), "and cleared afterwards"


def test_a_repo_that_writes_nothing_counts_as_failed(two_repo_ws, monkeypatch):
    """Trust the artefact, not the narration — an executor claiming success
    without writing _index.json has failed."""
    ws.add("w", two_repo_ws)

    async def fake_shot(*a, **k):
        return "All done! Context written successfully."

    monkeypatch.setattr(cb, "default_executor", lambda: "claude")
    from app import claude_cli
    monkeypatch.setattr(claude_cli, "one_shot", fake_shot)

    repo, ok, detail = asyncio.run(cb._build_one("w", "alpha"))
    assert ok is False
    assert "wrote no _index.json" in detail


def test_prompt_targets_the_right_directory_and_forbids_stray_writes(two_repo_ws):
    ws.add("w", two_repo_ws)
    p = cb._prompt("w", "alpha")
    assert str(cb.context_dir("w") / "repos" / "alpha") in p
    assert "_index.json" in p and "OVERVIEW.md" in p
    assert "verified_against" in p, "the drift join must be written"
    assert "Do not modify source" in p


def test_default_executor_falls_back_when_copilot_quota_is_down(monkeypatch):
    from app import claude_cli, tasks
    monkeypatch.setenv("ASTA_EXECUTOR", "copilot")
    monkeypatch.setattr(tasks, "_copilot_quota_down", lambda: True)
    monkeypatch.setattr(claude_cli, "available", lambda: True)
    assert cb.default_executor() == "claude"

    monkeypatch.setattr(tasks, "_copilot_quota_down", lambda: False)
    assert cb.default_executor() == "copilot"
