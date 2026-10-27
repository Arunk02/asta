"""A context refresh counts only when it updated this workspace's context and a
stale repo's verified_against actually moved; what moved is recorded."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app import refresh, store


def _ws(tmp_path, ctx=".contmark"):
    root = tmp_path / "booking-workspace"
    (root / ctx / "repos" / "svc").mkdir(parents=True)
    (root / ctx / "repos" / "svc" / "flow.md").write_text("old\n")
    run = lambda *a: subprocess.run(a, cwd=root, capture_output=True, text=True, check=True)  # noqa: E731
    run("git", "init", "-q"); run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    run("git", "add", "-A"); run("git", "commit", "-qm", "init")
    return root


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = _ws(tmp_path)

    class WS:
        path = root
        def exists(self):
            return True

    monkeypatch.setenv("ASTA_CONTEXT_DIRNAMES", ".contmark")   # as in his .env
    state = {"gaps": [], "edit": True, "told": []}
    monkeypatch.setattr(refresh.ws_mod, "get", lambda name: WS())
    monkeypatch.setattr(refresh, "context_gap", lambda ws: state["gaps"].pop(0))

    async def brain(prompt, cwd, timeout, effort):
        state["prompt"] = prompt
        if state["edit"]:
            (Path(cwd) / ".contmark" / "repos" / "svc" / "flow.md").write_text("new\n")
        return "svc/flow.md patched"

    async def _notify(text, *a, **k):
        state["told"].append(text)

    monkeypatch.setattr("app.claude_cli.one_shot", brain)
    monkeypatch.setattr(refresh.notify, "notify", _notify)
    state["root"] = root
    return state


def gap(verified, behind):
    return [{"repo": "svc", "verified": verified, "behind": behind}]


def test_the_workspaces_own_context_dir_is_edited_and_committed(world):
    world["gaps"] = [gap("aaaa", 105), gap("bbbb", 0)]
    out = asyncio.run(refresh.enrich("booking", "DRIFT svc"))
    assert ".contmark/ ONLY" in world["prompt"] and ".asta-context" not in world["prompt"]
    assert "1 file(s) updated, committed" in out and "outside" not in out
    log = subprocess.run(["git", "log", "--name-only", "-1", "--format=%s"], cwd=world["root"],
                         capture_output=True, text=True).stdout
    assert ".contmark/repos/svc/flow.md" in log
    assert "svc: verified aaaa → bbbb (105 behind → 0 behind)" in out
    assert store.kv_get("last_enriched:booking")


def test_files_changed_but_verified_against_did_not_move_is_not_fresh(world):
    world["gaps"] = [gap("aaaa", 105), gap("aaaa", 105)]
    out = asyncio.run(refresh.enrich("booking", "DRIFT svc"))
    assert "NOT brought up to date" in out and "svc (105 behind)" in out
    assert store.kv_get("last_enriched:booking") is None
    prov = json.loads(store.kv_get("context_provenance:booking"))
    assert prov["fresh"] is False and prov["files"] == [".contmark/repos/svc/flow.md"]


def test_a_run_that_changed_nothing_is_not_fresh(world):
    world["edit"] = False
    world["gaps"] = [gap("aaaa", 3), gap("aaaa", 3)]
    out = asyncio.run(refresh.enrich("booking", "DRIFT svc"))
    assert "NOT brought up to date" in out and "nothing under .contmark/ changed" in out
    assert store.kv_get("last_enriched:booking") is None


def test_provenance_records_each_repo_before_and_after(world):
    world["gaps"] = [gap("", -1), gap("cccc", 0)]
    asyncio.run(refresh.enrich("booking", "DRIFT svc"))
    prov = json.loads(store.kv_get("context_provenance:booking"))
    assert prov["fresh"] and prov["context_dir"] == ".contmark" and prov["committed"] == "committed"
    assert prov["repos"] == [{"repo": "svc", "verified_before": "", "verified_after": "cccc",
                              "behind_before": -1, "behind_after": 0}]
