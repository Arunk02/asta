"""Work-repo GitHub auth must not depend on the shell's active account."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from app import repo_ops


class _Process:
    def __init__(self, output: bytes = b"", rc: int = 0):
        self.output = output
        self.returncode = rc

    async def communicate(self, _input=None):
        return self.output, b""


@pytest.fixture
def work_repo(tmp_path, monkeypatch):
    repo = tmp_path / "booking"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/example-work-org/booking-service.git"],
                   check=True)
    monkeypatch.setenv("ASTA_GITHUB_WORK_OWNERS", "example-work-org,example-work-user")
    monkeypatch.setenv("ASTA_GITHUB_WORK_USER", "example-work-user")
    monkeypatch.setenv("GITHUB_TOKEN", "wrong-inherited-token")
    return repo


@pytest.mark.asyncio
async def test_work_repo_git_and_gh_use_office_keyring(work_repo, monkeypatch):
    calls = []

    async def run(*cmd, **kw):
        calls.append((cmd, kw))
        if cmd[:3] == ("gh", "auth", "token"):
            assert "GITHUB_TOKEN" not in kw["env"]
            return _Process(b"office-token\n")
        return _Process(b"ok")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", run)
    assert await repo_ops.git(work_repo, "git", "push", "origin", "feature/123") == (0, "ok")
    git_cmd, git_kw = calls[-1]
    assert git_cmd[:5] == ("git", "-c", "credential.helper=", "-c",
                           "credential.helper=!gh auth git-credential")
    assert git_kw["env"]["GH_TOKEN"] == git_kw["env"]["GITHUB_TOKEN"] == "office-token"

    assert await repo_ops.git(work_repo, "gh", "pr", "create", "--fill") == (0, "ok")
    gh_cmd, gh_kw = calls[-1]
    assert gh_cmd[:3] == ("gh", "pr", "create")
    assert gh_kw["env"]["GH_TOKEN"] == "office-token"


@pytest.mark.asyncio
async def test_personal_repo_does_not_inherit_office_shell_token(work_repo, monkeypatch):
    calls = []
    subprocess.run(["git", "-C", str(work_repo), "remote", "set-url", "origin",
                    "https://github.com/Arunk02/asta.git"], check=True)

    async def run(*cmd, **kw):
        calls.append((cmd, kw))
        return _Process(b"ok")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", run)
    assert await repo_ops.git(work_repo, "git", "push", "origin", "feature/123") == (0, "ok")
    cmd, kw = calls[-1]
    assert cmd[:3] == ("git", "push", "origin")
    assert "GH_TOKEN" not in kw["env"] and "GITHUB_TOKEN" not in kw["env"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_url_targets_work_account_even_from_personal_checkout(work_repo, monkeypatch):
    calls = []
    subprocess.run(["git", "-C", str(work_repo), "remote", "set-url", "origin",
                    "https://github.com/Arunk02/asta.git"], check=True)

    async def run(*cmd, **kw):
        calls.append((cmd, kw))
        return (_Process(b"office-token\n") if cmd[:3] == ("gh", "auth", "token")
                else _Process(b"ok"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", run)
    assert await repo_ops.git(work_repo, "gh", "pr", "view",
                              "https://github.com/example-work-org/booking-service/pull/1") == (0, "ok")
    assert calls[-1][1]["env"]["GH_TOKEN"] == "office-token"


@pytest.mark.asyncio
async def test_missing_office_login_fails_before_push(work_repo, monkeypatch):
    calls = []

    async def run(*cmd, **kw):
        calls.append(cmd)
        return _Process(rc=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", run)
    with pytest.raises(RuntimeError, match="GitHub login for example-work-user unavailable"):
        await repo_ops.git(work_repo, "git", "push", "origin", "feature/123")
    assert calls == [("gh", "auth", "token", "--user", "example-work-user")]


def test_office_login_matches_owner_not_similar_names(monkeypatch):
    monkeypatch.setenv("ASTA_GITHUB_WORK_OWNERS", "example-work-org")
    monkeypatch.setenv("ASTA_GITHUB_WORK_USER", "example-work-user")
    assert repo_ops.office_login("example-work-org/booking-service") == "example-work-user"
    assert repo_ops.office_login("example-work-org-fork/booking-service") == ""
