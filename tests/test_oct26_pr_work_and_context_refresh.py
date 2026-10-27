"""26 Oct: CT work on PR 1505 became a stranger task (#430) that could not check out
the branch #419 held; and an approved context refresh ran as a chat turn that may
not write a file, so the booking context stayed 27 days stale."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app import agent as agent_mod, capabilities, offers, refresh, store, tasks

PR = "telikos-booking-service: https://github.com/Maersk-Global/telikos-booking-service/pull/1505"


def _owner(status="awaiting_approval"):
    t = store.create_task("Move LD one-click check", "code", "x", "booking")
    store.update_task(t["id"], status=status, pr_urls=PR)
    return t["id"]


def test_the_task_that_shipped_a_pr_owns_it():
    tid = _owner()
    assert tasks.owner_of_pr("Fix CI failure on booking PR 1505")["id"] == tid
    assert tasks.owner_of_pr("Fix CI failure on booking PR 1499") is None
    assert tasks.owner_of_pr("Fix CI failure on AP PR 1505") is None


def test_a_merged_task_no_longer_owns_its_pr():
    _owner("merged")
    assert tasks.owner_of_pr("booking PR 1505 CT is red") is None


def test_pr_work_goes_to_its_task_not_a_new_one(monkeypatch):
    from app import relevance
    tid = _owner()
    monkeypatch.setattr(relevance, "guard_spawn", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "refinable_match", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "spawn", lambda *a, **k: pytest.fail("spawned a second task"))
    sent = []
    monkeypatch.setattr(tasks, "refine_soon", lambda task_id, text: sent.append((task_id, text)))
    capabilities.TURN_TEXT.set("Analyse on our booking PR why CT failed and fix it")
    try:
        out = agent_mod.delegate_task("Fix CI failure on booking PR 1505",
                                      "CT failed on booking PR 1505 — find why and fix it",
                                      kind="code", workspace="booking")
    finally:
        capabilities.TURN_TEXT.set("")
    assert f"task #{tid}" in out and sent and sent[0][0] == tid


# --- the context refresh is a job that can write ------------------------------------------

def _ws(tmp_path: Path) -> Path:
    root = tmp_path / "booking-workspace"
    (root / ".asta-context" / "repos" / "svc").mkdir(parents=True)
    (root / ".asta-context" / "repos" / "svc" / "flow.md").write_text("old\n")
    (root / "notes.txt").write_text("his\n")
    run = lambda *a: subprocess.run(a, cwd=root, capture_output=True, text=True, check=True)  # noqa: E731
    run("git", "init", "-q"); run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    run("git", "add", "-A"); run("git", "commit", "-qm", "init")
    return root


def test_an_approved_refresh_writes_commits_and_stamps(tmp_path, monkeypatch):
    root = _ws(tmp_path)

    class WS:
        path = str(root)
        def exists(self):
            return True

    monkeypatch.setattr(refresh.ws_mod, "get", lambda name: WS())

    async def brain(prompt, cwd, timeout, effort):
        assert ".asta-context/ ONLY" in prompt and "Enrichment contract" in prompt
        (Path(cwd) / ".asta-context" / "repos" / "svc" / "flow.md").write_text("new (source: A.java:3)\n")
        (Path(cwd) / "notes.txt").write_text("touched by mistake\n")
        return "svc/flow.md: patched the entry point"

    monkeypatch.setattr("app.claude_cli.one_shot", brain)
    told = []

    async def _notify(text, *a, **k):
        told.append(text)
    monkeypatch.setattr(refresh.notify, "notify", _notify)
    store.kv_set("last_enriched:booking", "1")
    out = asyncio.run(refresh.enrich("booking", "DRIFT svc: a..b"))
    assert "1 file(s) updated, committed" in out and "notes.txt" in out
    log = subprocess.run(["git", "log", "--name-only", "-1", "--format=%s"], cwd=root,
                         capture_output=True, text=True).stdout
    assert "Refresh booking workspace context" in log and ".asta-context/repos/svc/flow.md" in log
    assert "notes.txt" not in log                       # his file is never committed
    assert float(store.kv_get("last_enriched:booking")) > 1


class _Sink:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


@pytest.mark.asyncio
async def test_go_ahead_on_the_stale_offer_starts_the_job_not_a_chat_turn(monkeypatch):
    from app import main
    conv = store.create_conversation(model="claude_cli", workspace="booking")
    offers.propose(subject="🗂 booking context is stale", context="27 days",
                   question="Want me to bring the context up to date?", action="refresh it",
                   payload={"workspace": "booking", "enrich": True, "detail": "DRIFT svc: a..b"})
    started = []
    monkeypatch.setattr(refresh, "enrich_in_background", lambda ws, detail="": started.append((ws, detail)))
    monkeypatch.setattr(main, "_start_turn", lambda *a, **k: pytest.fail("ran as a chat turn"))
    sink = _Sink()
    assert await main._dispatch(conv, "go ahead", sink, "whatsapp") is None
    assert started == [("booking", "DRIFT svc: a..b")]
    assert "background" in " ".join(m.get("text", "") for m in sink.sent)
