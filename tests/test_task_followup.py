"""A follow-up on a finished analysis continues it (11 Nov, task 559).

"task N - … then what if in future same issue … fix it again?" read as an edit,
and a done analysis has no branch to refine, so it got "cannot be continued
(status: done)" — no answer, nothing recorded. Synthetic tasks only.
"""
from __future__ import annotations

import asyncio
import json

from app import main, store, tasks

SAID = ("task {id} - u telling no changes required now ? then what if in future "
        "same issue happens again , then we have to fix it again ?")


class Sink:
    def __init__(self):
        self.sent = []

    async def send(self, p):
        self.sent.append(p)

    def said(self):
        return " ".join(p.get("text") or "" for p in self.sent)


def _dispatch(text, monkeypatch):
    started, ran = [], []
    monkeypatch.setattr(main, "_start_turn", lambda c, t, s, ch: started.append(t))

    async def worker(task_id):
        ran.append(task_id)
    monkeypatch.setattr(tasks, "_worker", worker)
    conv = store.create_conversation("claude_cli", None)
    sink = Sink()

    async def go():
        await main._dispatch(conv, text, sink, "whatsapp", skip_new_gates=True)
        await asyncio.sleep(0)
    asyncio.run(go())
    return sink, started, ran, conv


def _analysis(status="done"):
    t = store.create_task("Diff the workflow code", "analysis", "Find what changed in the workflow", None)
    store.update_task(t["id"], status=status, result="The workflow code has not changed since August.")
    return t


def test_a_follow_up_on_a_done_analysis_continues_it(monkeypatch):
    t = _analysis()
    sink, started, ran, conv = _dispatch(SAID.format(id=t["id"]), monkeypatch)
    assert "continuing with your question" in sink.said() and "cannot be continued" not in sink.said()
    assert ran == [t["id"]] and not started
    row = store.get_task(t["id"])
    assert row["status"] == "running"
    assert "has not changed since August" in row["prompt"] and "fix it again" in row["prompt"]
    asked = json.loads(store.kv_get(f"task_asked:{t['id']}"))
    assert asked["his"].startswith("u telling no changes"), "his words, without the task number"
    said = [m["content"] for m in store.list_ui_messages(conv["id"])]
    assert any("fix it again" in m for m in said), "his message is kept"


def test_a_failed_analysis_continues_too(monkeypatch):
    t = _analysis(status="failed")
    sink, _, ran, _ = _dispatch(SAID.format(id=t["id"]), monkeypatch)
    assert ran == [t["id"]] and "continuing" in sink.said()


def test_a_second_follow_up_builds_on_the_original_ask_not_a_nested_prompt():
    t = _analysis()

    async def go(text):
        out = await tasks.continue_finished(t["id"], text)
        await asyncio.sleep(0)
        return out
    asyncio.run(go("first question"))
    store.update_task(t["id"], status="done", result="Second answer.")
    asyncio.run(go("second question"))
    prompt = store.get_task(t["id"])["prompt"]
    assert prompt.count("Find what changed in the workflow") == 1
    assert "Second answer." in prompt and "second question" in prompt and "first question" not in prompt


def test_a_merged_code_task_is_still_not_continued(monkeypatch):
    t = store.create_task("Skip the tracker call", "code", "p", "ws")
    store.update_task(t["id"], status="merged")
    sink, started, ran, _ = _dispatch(SAID.format(id=t["id"]), monkeypatch)
    assert "cannot be continued" in sink.said() and not ran


# --- "minor changes on top of an already merged PR" ---------------------------------

class FakeTrees:
    """The workspace checkouts, without git: what was created, on which branch."""

    def __init__(self, monkeypatch, tmp_path):
        from app import worktrees
        self.made, self.removed, self.live = [], [], set()
        monkeypatch.setattr(tasks, "code_cwd", lambda ws=None: str(tmp_path))
        monkeypatch.setattr(worktrees, "exists", lambda root, tid: tid in self.live)

        async def create(root, tid, branch, *hints, existing=None):
            self.made.append((tid, branch, hints))
            self.live.add(tid)
            return [{"repo": "svc-booking", "branch": branch, "ok": True, "note": ""}]

        async def remove(tid, t=None):
            self.removed.append(tid)
            self.live.discard(tid)
            return []
        monkeypatch.setattr(worktrees, "create", create)
        monkeypatch.setattr(tasks, "remove_checkout", remove)


def _merged(branch="feature/TEST-1"):
    t = store.create_task("Skip the tracker call", "code", "p", "ws")
    store.update_task(t["id"], status="merged", pr_state="MERGED",
                      pr_urls="svc-booking: https://example.test/pr/9")
    store.kv_set(f"task_branch:{t['id']}", branch)
    store.kv_set(f"worktree_cleaned:{t['id']}", json.dumps({"branch": branch, "repos": ["svc-booking"]}))
    return t


def test_a_merged_task_takes_a_follow_up_on_a_fresh_branch(monkeypatch, tmp_path):
    trees = FakeTrees(monkeypatch, tmp_path)
    seen = {}

    async def worker(task_id, prompt, approved=False):
        seen["prompt"] = prompt
    monkeypatch.setattr(tasks, "_resume_worker", worker)
    t = _merged()

    async def go():
        out = await tasks.refine(t["id"], "also skip it for grey pool orders")
        await asyncio.sleep(0)
        return out
    said = asyncio.run(go())
    row = store.get_task(t["id"])
    assert trees.made == [(t["id"], "feature/TEST-1-followup-1", ("svc-booking",))]
    assert store.kv_get(f"task_branch:{t['id']}") == "feature/TEST-1-followup-1"
    assert row["status"] == "running" and row["pr_urls"] == ""
    assert "pr/9" in store.kv_get(f"task_merged_prs:{t['id']}"), "the merged PR is remembered"
    assert "MERGED into the base branch" in seen["prompt"] and "grey pool" in seen["prompt"]
    assert "fresh branch" in said and "ship" in said


def test_a_second_follow_up_gets_the_next_branch_not_a_longer_name(monkeypatch, tmp_path):
    trees = FakeTrees(monkeypatch, tmp_path)

    async def worker(task_id, prompt, approved=False):
        return None
    monkeypatch.setattr(tasks, "_resume_worker", worker)
    t = _merged()
    asyncio.run(tasks.refine(t["id"], "also skip it for grey pool orders"))
    store.update_task(t["id"], status="merged")
    asyncio.run(tasks.refine(t["id"], "and add a log line"))
    assert [b for _, b, _ in trees.made] == ["feature/TEST-1-followup-1", "feature/TEST-1-followup-2"]


def test_a_merged_task_named_from_his_phone_lands_on_that_task(monkeypatch, tmp_path):
    FakeTrees(monkeypatch, tmp_path)

    async def worker(task_id, prompt, approved=False):
        return None
    monkeypatch.setattr(tasks, "_resume_worker", worker)
    t = _merged()
    before = len(store.list_tasks(limit=50))
    sink, started, ran, _ = _dispatch(f"task {t['id']} also add the grey pool check", monkeypatch)
    # "add …" plans first, as on any done task — on THIS task, on its follow-up branch.
    assert "cannot be continued" not in sink.said() and f"Task #{t['id']}" in sink.said()
    assert store.kv_get(f"task_branch:{t['id']}") == "feature/TEST-1-followup-1"
    assert len(store.list_tasks(limit=50)) == before, "no new task"
    assert not started
