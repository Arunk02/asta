"""Night refresh of a layered workspace (app/context_refresh.py). Synthetic repos and a bare remote;
no network, no model (the gardener is a stub)."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import asyncio

import pytest

from app import claude_cli, context_refresh, store

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def ws(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    root = tmp_path / "demo-workspace"
    (root / ".asta-context").mkdir(parents=True)
    (root / ".asta-context" / "services.json").write_text(json.dumps({"alpha": "alpha-service"}))
    # stub ctx.js / check-drift.js: verify fails while a marker file says so
    (root / ".asta-context" / "ctx.js").write_text(
        "const fs=require('fs'),p=require('path');const c=process.argv[2];"
        "if(c==='index'){console.log('indexed 1 files: alpha origin/develop@abc1234');process.exit(0)}"
        "if(c==='verify'){const m=p.join(__dirname,'BROKEN');"
        "if(fs.existsSync(m)&&!fs.readFileSync(p.join(__dirname,'..','services','alpha','CLAUDE.md'),'utf8').includes('fixed'))"
        "{console.log('FAIL services/alpha/CLAUDE.md: path `x/y` not in alpha');process.exit(1)}console.log('context ok')}")
    (root / ".asta-context" / "check-drift.js").write_text("process.exit(0)")
    (root / "CLAUDE.md").write_text("# root\n")
    (root / "services" / "alpha").mkdir(parents=True)
    (root / "services" / "alpha" / "CLAUDE.md").write_text("# alpha\n")
    (root / ".gitignore").write_text("/alpha-service/\n.asta-context/BROKEN\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _git(root, "add", "-A"); _git(root, "commit", "-qm", "init")
    _git(root, "remote", "add", "origin", str(remote)); _git(root, "push", "-q", "origin", "main")
    repo = root / "alpha-service"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t"); monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t"); monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")
    monkeypatch.setattr(claude_cli, "workspace_roots", lambda: [str(root)])
    return root, remote


def _remote_head(remote: Path) -> str:
    return subprocess.run(["git", "--git-dir", str(remote), "log", "-1", "--format=%s", "main"],
                          capture_output=True, text=True).stdout.strip()


def test_window_wraps_midnight(monkeypatch):
    monkeypatch.setenv("ASTA_REFRESH_WINDOW", "22:00-06:00")
    at = lambda h: time.mktime((2026, 11, 26, h, 0, 0, 0, 0, -1))
    assert context_refresh.in_window(at(23)) and context_refresh.in_window(at(3))
    assert not context_refresh.in_window(at(6)) and not context_refresh.in_window(at(18))


def test_scan_flags_secrets_and_live_ids_in_added_lines_only():
    diff = "+password = hunter2hunter2\n+booking ABCD1234567 reached\n-api_key=oldoldold1\n+plain line"
    bad = context_refresh.scan(diff)
    assert len(bad) == 2 and bad[0].startswith("secret") and bad[1].startswith("id")
    assert context_refresh.scan("+`BookingEventsWorkflowImplementation` continues\n") == []


def test_drift_rotates_a_few_docs_per_night(monkeypatch):
    monkeypatch.setattr(context_refresh, "DRIFT_PER_NIGHT", 2)
    report = "DRIFT r1: a..b\n  stale mini-skills: x.md, y.md, z.md\n"
    one = context_refresh._drift_items(report)
    two = context_refresh._drift_items(report)
    assert len(one) == 2 and len(two) == 2 and one != two
    assert all(".asta-context/repos/r1/" in d for d in one + two)


def test_blocked_refresh_defers(ws, monkeypatch):
    root, _ = ws
    async def why():
        return ["outside the night window"]
    monkeypatch.setattr(context_refresh, "blockers", why)
    out = asyncio.run(context_refresh.refresh("demo", root))
    assert out.startswith("deferred") and store.kv_get("context_refresh_pending:demo") == "1"


def test_clean_night_pushes_nothing_and_says_so(ws):
    root, remote = ws
    line = asyncio.run(context_refresh.refresh("demo", root, force=True))
    assert "nothing to commit" in line and "fetch failed" in line
    assert _remote_head(remote) == "init"
    assert context_refresh.brief_line(time.time() - 60) == line


def test_a_problem_is_fixed_verified_and_pushed(ws, monkeypatch):
    root, remote = ws
    (root / ".asta-context" / "BROKEN").write_text("1")
    async def gardener(r, problems):
        assert any("not in alpha" in p for p in problems)
        (r / "services" / "alpha" / "CLAUDE.md").write_text("# alpha\nfixed: path updated\n")
        return "- services/alpha/CLAUDE.md: path fixed"
    monkeypatch.setattr(context_refresh, "_gardener", gardener)
    line = asyncio.run(context_refresh.refresh("demo", root, force=True))
    assert "pushed" in line and "fixed" in line
    assert _remote_head(remote) == "Refresh workspace context against develop"


def test_gardener_outside_its_paths_is_reverted_and_nothing_pushed(ws, monkeypatch):
    root, remote = ws
    (root / ".asta-context" / "BROKEN").write_text("1")
    async def gardener(r, problems):
        (r / "services" / "alpha" / "CLAUDE.md").write_text("# alpha\nfixed\n")
        (r / "README.md").write_text("rewritten\n")
        return "x"
    monkeypatch.setattr(context_refresh, "_gardener", gardener)
    line = asyncio.run(context_refresh.refresh("demo", root, force=True))
    assert "touched README.md" in line and _remote_head(remote) == "init"


def test_a_secret_in_the_fix_is_never_pushed(ws, monkeypatch):
    root, remote = ws
    (root / ".asta-context" / "BROKEN").write_text("1")
    async def gardener(r, problems):
        (r / "services" / "alpha" / "CLAUDE.md").write_text("# alpha\nfixed\ntoken = abcdefghijkl\n")
        return "x"
    monkeypatch.setattr(context_refresh, "_gardener", gardener)
    line = asyncio.run(context_refresh.refresh("demo", root, force=True))
    assert "scan flagged" in line and _remote_head(remote) == "init"
