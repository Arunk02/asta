"""Temporal entry points are found by annotation, wherever the repo keeps them."""
from __future__ import annotations

import json
import subprocess

import pytest

from app import refresh

PKG = "workflow/src/main/java/com/acme/plan"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    repo = root / "plan-service"
    files = {
        f"{PKG}/activity/AmendActivity.java": "@ActivityInterface\npublic interface AmendActivity {}",
        f"{PKG}/activity/AmendActivityImpl.java": "public class AmendActivityImpl implements AmendActivity {}",
        f"{PKG}/activity/CancelActivity.java": "@ActivityInterface\npublic interface CancelActivity {}",
        f"{PKG}/activity/CancelActivityImpl.java":
            "public class CancelActivityImpl extends Base implements Serializable, CancelActivity {}",
        f"{PKG}/flow/PlanWorkflow.java": "@WorkflowInterface\npublic interface PlanWorkflow {}",
        f"{PKG}/flow/PlanFlowRunner.java": "public class PlanFlowRunner implements PlanWorkflow {}",
        f"{PKG}/util/Helper.java": "public class Helper implements Runnable {}",
        # Dead code: the whole class is commented out, so it is not an entry point.
        f"{PKG}/activity/DeadActivityImpl.java":
            "// public class DeadActivityImpl implements AmendActivity {}\n"
            "/*\npublic class DeadActivityImpl implements AmendActivity {\n}\n*/",
        "workflow/src/test/java/com/acme/plan/FakeAmend.java": "class FakeAmend implements AmendActivity {}",
    }
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    ctx = root / ".asta-context"
    ctx.mkdir(parents=True)
    # Docs name the INTERFACE for one activity: that counts as mapped.
    (ctx / "_global_index.json").write_text(json.dumps({"files": [{"entities": ["CancelActivity"]}]}))

    class WS:
        path = root
        def exists(self):
            return True

    monkeypatch.setattr(refresh.ws_mod, "get", lambda name: WS())
    from app import worktrees
    monkeypatch.setattr(worktrees, "repos_in", lambda r: [repo])
    refresh._coverage_cache.clear()
    return root


def test_activities_and_workflows_are_found_by_annotation_not_folder(ws):
    cov, = refresh.coverage_gap("x")
    assert cov["total"] == 3
    assert cov["kinds"] == {"AmendActivityImpl": "temporal activity", "PlanFlowRunner": "temporal workflow"}


def test_declaring_the_interface_maps_its_implementation(ws):
    cov, = refresh.coverage_gap("x")
    assert "CancelActivityImpl" not in cov["missing"] and cov["mapped"] == 1


def test_interfaces_tests_and_unrelated_classes_are_not_entry_points(ws):
    cov, = refresh.coverage_gap("x")
    for name in ("AmendActivity", "PlanWorkflow", "FakeAmend", "Helper"):
        assert name not in cov["missing"]


def test_a_class_commented_out_whole_is_not_an_entry_point(ws):
    assert "DeadActivityImpl" not in refresh._temporal_impls(ws / "plan-service")
    assert "AmendActivityImpl" in refresh._temporal_impls(ws / "plan-service")
