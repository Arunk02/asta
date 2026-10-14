"""14 Oct audit, P4: code tasks across repos, on the right branch, with context, on CI's JDK.

  · #268 (8 Oct) was blocked: "event router library" never found
    ~/Projects/telikos-event-router-library — only the exact name did;
  · #295 (13 Oct) was cut from develop and could not get PR 676's branch;
  · worktrees under ~/.asta-worktrees saw none of the workspace's context;
  · JDK 25 became Maven's default and Lombok stopped compiling locally.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from app import buildenv, store, task_fence, tasks, workspace_tools, worktrees


@pytest.mark.parametrize("words,hit", [
    ("event router library PR 95 revert", True),
    ("event-router-library: make non-reactive optional", True),
    ("bump eventrouterlibrary", True),
    ("telikos-event-router-library", True),
    ("the event library issue", False),
    ("new customs status changes", False),
])
def test_an_outside_repo_is_found_by_how_people_say_it(tmp_path, monkeypatch, words, hit):
    proj = tmp_path / "Projects"
    for name in ("telikos-event-router-library", "new"):
        repo = proj / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin",
                        f"https://github.com/Maersk-Global/{name}.git"], cwd=repo, check=True)
    monkeypatch.setattr(worktrees, "REPO_DIRS", str(proj))
    found = [p.name for p in worktrees.outside(words)]
    assert (found == ["telikos-event-router-library"]) is hit, found


def test_a_one_word_repo_never_matches_as_a_phrase():
    assert worktrees._phrase("new") == ""
    assert worktrees._phrase("telikos-booking") == ""


def test_builds_run_on_the_pinned_jdk(monkeypatch):
    buildenv.java_home.cache_clear()
    monkeypatch.setenv("ASTA_JAVA_HOME", "/jdk21")
    env = buildenv.with_java({"PATH": "/usr/bin"})
    assert env["JAVA_HOME"] == "/jdk21" and env["PATH"].startswith("/jdk21/bin")
    fenced = task_fence.env({"PATH": "/usr/bin"}, mode="code")
    assert fenced["JAVA_HOME"] == "/jdk21"
    assert fenced["PATH"].startswith(str(task_fence.SHIMS)), "the write fence stays first"
    assert "JAVA_HOME" not in task_fence.env({"PATH": "/usr/bin"}, mode="")
    buildenv.java_home.cache_clear()


def test_no_jdk_found_leaves_the_env_alone(monkeypatch):
    buildenv.java_home.cache_clear()
    monkeypatch.setattr(buildenv, "java_home", lambda: "")
    assert buildenv.with_java({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


def test_the_verify_check_builds_on_the_pinned_jdk(monkeypatch):
    from app import verify
    seen = {}

    async def shell(cmd, cwd=None, env=None, **kw):
        seen["env"] = env
        raise OSError("stop")

    monkeypatch.setattr(verify.asyncio, "create_subprocess_shell", shell)
    monkeypatch.setattr(buildenv, "java_home", lambda: "/jdk21")
    asyncio.run(verify.run("/tmp", "mvn -q test"))
    assert seen["env"]["JAVA_HOME"] == "/jdk21"


def _workspace(tmp_path, monkeypatch):
    root = tmp_path / "booking-workspace"
    (root / ".asta-context").mkdir(parents=True)
    (root / ".asta-context" / "boot.sh").write_text("echo boot\n")
    (root / "AGENTS.md").write_text("# rules\n")
    monkeypatch.setattr(workspace_tools, "WORKSPACES", {"booking": root})
    return root


def test_a_checkout_outside_the_workspace_is_pointed_at_its_context(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    note = tasks.workspace_context_note("booking", str(tmp_path / "asta-worktrees" / "task-1"))
    assert str(root / ".asta-context" / "boot.sh") in note and str(root / "AGENTS.md") in note


def test_a_checkout_inside_the_workspace_needs_no_note(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    assert tasks.workspace_context_note("booking", str(root / ".asta-worktrees" / "task-1")) == ""


def test_the_boot_is_not_repeated_when_the_prompt_already_has_it(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    note = tasks.workspace_context_note("booking", str(tmp_path / "elsewhere"),
                                        f"sh {root / '.asta-context' / 'boot.sh'}")
    assert "boot.sh" not in note and "AGENTS.md" in note


def test_every_code_leg_gets_the_note(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)
    seen = {}

    async def leg(task_id, prompt, cwd, **kw):
        seen["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(tasks, "_code_leg", leg)
    monkeypatch.setattr(tasks, "refuse_shared_tree", lambda *a: None)

    async def trees(*a):
        return {}

    monkeypatch.setattr(tasks, "other_trees", trees)
    asyncio.run(tasks._run_code_leg(1, "fix it", str(tmp_path / "out"), resume=False,
                                    effort="", workspace="booking"))
    assert "[Workspace context]" in seen["prompt"]


# --- a task on a PR works ON that PR's branch -----------------------------------------------

def _origin_with_pr_branch(tmp_path):
    origin = tmp_path / "telikos-email-service.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    run = lambda *a: subprocess.run(a, cwd=seed, check=True, capture_output=True)  # noqa: E731
    run("git", "config", "user.email", "t@e.com"); run("git", "config", "user.name", "T")
    run("git", "checkout", "-q", "-b", "develop")
    (seed / "pom.xml").write_text("develop\n"); run("git", "add", "-A"); run("git", "commit", "-qm", "base")
    run("git", "push", "-q", "origin", "develop")
    run("git", "checkout", "-q", "-b", "release/temporal-cert-dev-spt")
    (seed / "pom.xml").write_text("the PR's own change\n"); run("git", "commit", "-qam", "pr")
    run("git", "push", "-q", "origin", "release/temporal-cert-dev-spt")
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "clone", "-q", str(origin), str(ws / "telikos-email-service")],
                   check=True, capture_output=True)
    return ws


def test_the_worktree_is_the_pr_branch_as_it_is_on_origin(tmp_path):
    ws = _origin_with_pr_branch(tmp_path)
    out = asyncio.run(worktrees.create(ws, 77, "feature/task-77",
                                       existing={"telikos-email-service": "release/temporal-cert-dev-spt"}))
    assert out[0]["ok"], out
    path = Path(out[0]["path"])
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()
    assert head == "release/temporal-cert-dev-spt"
    assert (path / "pom.xml").read_text() == "the PR's own change\n", "never a new branch from develop"


def test_without_a_pr_the_task_gets_its_own_branch_from_develop(tmp_path):
    ws = _origin_with_pr_branch(tmp_path)
    out = asyncio.run(worktrees.create(ws, 78, "feature/task-78"))
    path = Path(out[0]["path"])
    assert (path / "pom.xml").read_text() == "develop\n"


def test_a_merged_or_unknown_pr_names_no_branch(monkeypatch, tmp_path):
    assert asyncio.run(tasks.pr_head_branch({"title": "plain task", "prompt": ""}, tmp_path)) == {}


def test_generic_words_never_name_a_repo():
    assert worktrees._phrase("telikos-booking-service") == ""
    assert worktrees._phrase("telikos-event-router-library") != ""


def test_the_pinned_jdk_is_the_highest_21_not_the_one_lombok_fails_on():
    """The bench records Lombok 1.18.30 failing on Corretto 21.0.4; 21.0.11 built
    the email service clean on 14 Oct. `java_home -v 21` returns the highest 21."""
    import os
    import subprocess
    if not os.path.exists("/usr/libexec/java_home"):
        pytest.skip("not macOS — no java_home here")
    buildenv.java_home.cache_clear()
    out = subprocess.run(["/usr/libexec/java_home", "-v", "21"], capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("no JDK 21 on this machine")
    assert buildenv.java_home() == out.stdout.strip()
    assert "21.0.4" not in buildenv.java_home() or "21.0.11" not in out.stdout
    buildenv.java_home.cache_clear()
