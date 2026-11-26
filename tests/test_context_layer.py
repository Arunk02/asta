"""The workspace's CLAUDE.md layer on code legs (app/context_layer.py) and the tests-on-stop gate.

Synthetic workspace and repos only: nothing here reads his real checkouts."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import claude_cli, context_layer, tasks
from app.hooks import tests_on_stop


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "demo-workspace"
    (root / ".asta-context").mkdir(parents=True)
    (root / ".asta-context" / "services.json").write_text(json.dumps({"alpha": "alpha-service"}))
    (root / ".asta-context" / "ctx.js").write_text("// stub")
    (root / "CLAUDE.md").write_text("# root\n")
    (root / "services" / "alpha").mkdir(parents=True)
    (root / "services" / "alpha" / "CLAUDE.md").write_text("# alpha\n")
    repo = root / "alpha-service"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("ASTA_WORKTREES_DIR", str(tmp_path / "wts"))
    monkeypatch.delenv("ASTA_CONTEXT_LAYER", raising=False)
    monkeypatch.setattr(claude_cli, "workspace_roots", lambda: [str(root)])
    return root


def _task_checkout(ws: Path, n: int = 7) -> Path:
    from app import worktrees
    task = worktrees.base_for(ws) / f"task-{n}"
    repo = task / "alpha-service"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "CLAUDE.md").write_text("team file\n")
    return task


def test_a_workspace_has_the_layer_only_with_both_files(ws):
    assert context_layer.services(ws) == {"alpha": "alpha-service"}
    assert context_layer.active(ws)
    (ws / "CLAUDE.md").unlink()
    assert not context_layer.active(ws)


def test_the_layer_is_found_from_the_workspace_and_its_task_checkouts(ws, tmp_path, monkeypatch):
    task = _task_checkout(ws)
    assert context_layer.root_for_cwd(ws / "alpha-service") == ws.resolve()
    assert context_layer.root_for_cwd(task) == ws.resolve()
    assert context_layer.root_for_cwd(tmp_path / "elsewhere") is None
    monkeypatch.setenv("ASTA_CONTEXT_LAYER", "0")
    assert context_layer.root_for_cwd(task) is None


def test_service_files_are_linked_excluded_and_never_overwrite_a_real_file(ws):
    task = _task_checkout(ws)
    assert context_layer.link_service_files(task, ws) == ["alpha-service"]
    link = task / "alpha-service" / "CLAUDE.local.md"
    assert link.is_symlink() and link.read_text() == "# alpha\n"
    exclude = (task / "alpha-service" / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count("CLAUDE.local.md") == 1
    assert context_layer.link_service_files(task, ws) == []          # idempotent
    link.unlink()
    link.write_text("his own notes")
    assert context_layer.link_service_files(task, ws) == []
    assert link.read_text() == "his own notes"


def test_excludes_cover_his_checkout_and_every_task_checkout(ws):
    from app import worktrees
    ex = context_layer.excludes(ws)
    assert str(ws.resolve() / "alpha-service" / "CLAUDE.md") in ex
    assert f"{worktrees.base_for(ws).resolve()}/*/alpha-service/CLAUDE.md" in ex
    assert not any(e.endswith("CLAUDE.local.md") for e in ex)


def test_leg_settings_carry_excludes_and_the_stop_gate_on_build_legs_only(ws, monkeypatch):
    monkeypatch.delenv("ASTA_STOP_TESTS", raising=False)
    build = json.loads(claude_cli.leg_settings(True, False, ws))
    assert build["claudeMdExcludes"] == context_layer.excludes(ws)
    assert f"Edit(/{ws.resolve()}/alpha-service/**)" in build["permissions"]["deny"]
    assert build["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("tests_on_stop.py")
    plan = json.loads(claude_cli.leg_settings(True, True, ws))
    assert "Stop" not in plan["hooks"] and plan["claudeMdExcludes"]
    assert claude_cli.leg_settings(False, False, ws) == claude_cli.OUTPUT_SETTINGS
    monkeypatch.setenv("ASTA_STOP_TESTS", "0")
    assert "Stop" not in json.loads(claude_cli.leg_settings(True, False, ws)).get("hooks", {})


def test_the_layer_env_var_is_set_only_with_a_layer(ws):
    assert context_layer.ENV_ADDITIONAL_MD not in claude_cli._layer_env({"A": "1"}, None)
    assert claude_cli._layer_env({"A": "1"}, ws)[context_layer.ENV_ADDITIONAL_MD] == "1"


def test_the_boot_step_becomes_one_ctx_call(ws, monkeypatch):
    prompt = "task text" + tasks.CODE_OVERRIDES
    assert "boot.sh" in prompt
    out = context_layer.rewrite_boot(prompt, ws)
    assert "boot.sh" not in out.split("CONTEXT USED")[0]
    assert f"node {ws / '.asta-context' / 'ctx.js'} find" in out
    monkeypatch.setenv("ASTA_CONTEXT_LAYER", "0")
    assert context_layer.rewrite_boot(prompt, ws) == prompt


def test_a_leg_outside_the_workspace_gets_a_pointer_not_the_boot_note(ws, monkeypatch):
    task = _task_checkout(ws)
    monkeypatch.setattr(tasks, "code_cwd", lambda _w: str(ws))
    note = tasks.workspace_context_note("demo", str(task))
    assert "CLAUDE.md" in note and "ctx.js find" in note and "boot.sh" not in note
    assert len(note.replace(str(ws), "")) < 300


def test_mismatches_are_read_from_a_reply():
    reply = ("Done.\n- `stack/build-and-test.md` → says mvn test -pl service, code does need -f service/pom.xml\n"
             "- no other notes")
    found = context_layer.mismatches(reply)
    assert found == [{"doc": "stack/build-and-test.md", "says": "mvn test -pl service",
                      "code": "need -f service/pom.xml"}]


def _transcript(tmp_path: Path, steps) -> Path:
    lines = []
    for i, (kind, val) in enumerate(steps):
        if kind == "edit":
            lines.append({"message": {"content": [{"type": "tool_use", "id": f"e{i}", "name": "Edit",
                                                     "input": {"file_path": val}}]}})
        else:
            cmd, body = val
            lines.append({"message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                                     "input": {"command": cmd}}]}})
            lines.append({"message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                     "content": body}]}})
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines))
    return p


PASS = ("mvn -f service/pom.xml clean test -Dtest=FooTest > /tmp/b.log 2>&1; tail -5 /tmp/b.log",
        "Tests run: 4, Failures: 0, Errors: 0\nBUILD SUCCESS")
FAIL = (PASS[0], "Tests run: 4, Failures: 1\nBUILD FAILURE")
ZERO = (PASS[0], "Tests run: 0, Failures: 0\nBUILD SUCCESS")


@pytest.mark.parametrize("steps,owes", [
    ([], False),
    ([("edit", "README.md")], False),
    ([("edit", "src/Foo.java")], True),
    ([("edit", "src/Foo.java"), ("test", PASS)], False),
    ([("edit", "src/Foo.java"), ("test", FAIL)], True),
    ([("edit", "src/Foo.java"), ("test", ZERO)], True),
    ([("test", PASS), ("edit", "src/Foo.java")], True),
    ([("edit", "src/Foo.java"), ("test", ("mvn -DskipTests package", "BUILD SUCCESS"))], True),
])
def test_stop_gate_verdicts(tmp_path, steps, owes):
    v = tests_on_stop.verdict(tests_on_stop._events(_transcript(tmp_path, steps)))
    assert bool(v) is owes


def test_stop_gate_blocks_then_gives_up(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    t = _transcript(tmp_path, [("edit", "src/Foo.java")])
    event = json.dumps({"session_id": "s-synthetic-1", "transcript_path": str(t)})
    codes = [subprocess.run([sys.executable, str(claude_cli.TESTS_ON_STOP)], input=event,
                            capture_output=True, text=True, timeout=20, env={"TMPDIR": str(tmp_path),
                                                                             "PATH": "/usr/bin:/bin"}).returncode
             for _ in range(4)]
    assert codes == [2, 2, 2, 0]
    again = subprocess.run([sys.executable, str(claude_cli.TESTS_ON_STOP)], input=event, capture_output=True,
                           text=True, timeout=20, env={"TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert again.returncode == 2          # a later leg on the same session is gated afresh
