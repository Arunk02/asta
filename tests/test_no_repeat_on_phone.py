"""The same words never reach his phone twice. 8 Oct.

"Ship task 268" was answered in his chat; ship() had pushed the same "🔀 Task #268
shipped" line, it rode the batch and arrived again two minutes later. Over two
days: "#257 shipped" three times, a meeting heads-up twice in one minute.
"""

from __future__ import annotations

import asyncio

import pytest

from app import delivery, notify, store

SHIPPED = ("🔀 Task #268 shipped: • telikos-event-router-library: event-router-library "
           "PR 95 — https://github.com/Maersk-Global/telikos-event-router-library/pull/95")


@pytest.fixture
def phone(monkeypatch):
    sent: list[str] = []

    async def wa(text, done=False):
        sent.append(text)
        notify._note_on_phone(text)          # what the real bridge send records
        return True

    async def tg(text):
        return False
    monkeypatch.setattr(notify, "wa_send", wa)
    monkeypatch.setattr(notify.telegram, "send", tg)
    monkeypatch.setattr(notify, "SAID_WINDOW", 1800)
    return sent


def test_a_line_already_in_his_chat_reply_is_not_pushed_from_the_batch(phone):
    delivery.buffer(SHIPPED)                 # pushed by ship(), waiting in the batch
    notify.note_reply(SHIPPED)               # …and the chat reply said the same
    out = asyncio.run(delivery.flush_buffered())
    assert out["items"] == 0 and phone == []


def test_the_same_push_twice_goes_once(phone):
    heads_up = "📅 In 32 min — Code Refactor (by Vinish Kumar), 12:30 PM"
    asyncio.run(notify.deliver(heads_up))
    out = asyncio.run(notify.notify(heads_up, "premeeting", considered=True))
    assert out.get("duplicate") and phone == [heads_up]


def test_a_reminder_he_set_still_rings_again(phone):
    asyncio.run(notify.deliver("⏰ Reminder: call the CAB about the release window"))
    out = asyncio.run(notify.notify("⏰ Reminder: call the CAB about the release window",
                                    "reminder", asked=True, considered=True))
    assert not out.get("duplicate")


def test_news_beyond_an_earlier_line_still_goes(phone):
    notify.note_reply("Task #268 shipped: event-router-library PR 95")
    longer = ("Task #268 shipped: event-router-library PR 95 — and CI just went red on "
              "the component test, the context did not start")
    assert not notify.already_on_phone(longer)
    assert notify.already_on_phone("Task #268 shipped: event-router-library PR 95")


def test_a_held_item_already_said_is_dropped_from_the_release(phone, monkeypatch):
    notify._hold(SHIPPED, ("k1",))
    notify._hold("🧪 CI on booking PR 1470 is green — all 6 checks passed.", ("k2",))
    notify.note_reply(SHIPPED)
    monkeypatch.setattr(notify, "answered", lambda keys: False)
    monkeypatch.setattr(delivery, "quiet_now", lambda: False)
    asyncio.run(notify.flush_held())
    assert len(phone) == 1 and "CI on booking PR 1470" in phone[0] and "#268" not in phone[0]


def test_a_draft_stored_for_him_but_never_sent_does_not_count(phone):
    cid = store.create_conversation(model="claude_cli", workspace=None)["id"]
    store.kv_set("wa_conversation", cid)
    store.add_ui_message(cid, "assistant", "Draft for Vinish (teams): bro can u review PR 95?", {})
    assert not notify.already_on_phone("Draft for Vinish (teams): bro can u review PR 95?")


def test_off_when_the_window_is_zero(phone, monkeypatch):
    notify.note_reply(SHIPPED)
    monkeypatch.setattr(notify, "SAID_WINDOW", 0)
    assert not notify.already_on_phone(SHIPPED)


def test_a_pr_is_titled_from_its_change_not_its_branch(monkeypatch, tmp_path):
    """PR 95 went up as "feature/268 revert event library change to unblock e":
    with several commits, `gh pr create --fill` falls back to the branch name."""
    from app import tasks, worktrees
    ran: list[tuple] = []

    async def git(cwd, *args, **k):
        ran.append(args)
        if args[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return 0, "feature/asta-268-revert-event-library-change-to-unblock-e\n"
        if args[:2] == ("git", "log"):
            return 0, "2b7510e SNAPSHOT\n1d40b0c context test\nadeb881 Make configs optional\n"
        return 0, ""

    async def base(repo):
        return "origin/main"

    async def verified(repo, cur, base_ref, **k):
        return "https://github.com/acme/lib/pull/95"

    async def no_other(*a, **k):
        return []
    monkeypatch.setattr(tasks.repo_ops, "git", git)
    monkeypatch.setattr(worktrees, "repos_in", lambda root: [tmp_path / "lib"])
    monkeypatch.setattr(worktrees, "_base_branch", base)
    monkeypatch.setattr(tasks, "_verified_pr", verified)
    monkeypatch.setattr(tasks, "_other_bases", no_other)
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: str(tmp_path))
    t = store.create_task("Revert event library change", "code", "p", "booking")
    store.update_task(t["id"], status="done")
    asyncio.run(tasks.ship(t["id"]))
    create = next(a for a in ran if a[:3] == ("gh", "pr", "create"))
    assert "--fill-first" in create and "--fill" not in create
