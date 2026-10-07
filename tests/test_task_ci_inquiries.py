"""Checking an existing task is not feedback authorizing another implementation."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import agent, capabilities, frontdesk, loop, main, offers, ops, repo_ops, store, tasks


class Sink:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    @property
    def text(self):
        return "\n".join(m.get("text", "") for m in self.sent)


def shipped(conv=None):
    t = store.create_task("Fix booking amend flow", "code", "fix", "booking")
    store.update_task(
        t["id"], status="shipped", finished_at=time.time(),
        pr_urls="booking: https://github.com/work/booking/pull/1466",
        pr_state="green/REVIEW_REQUIRED")
    if conv:
        tasks.link_task(conv["id"], t["id"])
    return t["id"]


def conversation():
    return store.create_conversation(model="claude_cli", workspace="booking")


def test_checking_an_unrelated_subject_is_not_the_running_tasks_ci():
    conv = conversation()
    task = store.create_task("Booking fix", "code", "fix", "booking")
    store.update_task(task["id"], status="running")
    tasks.link_task(conv["id"], task["id"])

    assert frontdesk.ci_inquiry("check the consumer lag on the AP side") == (False, False)
    assert main._task_inquiry_target(conv["id"], "check the consumer lag on the AP side") is None
    assert frontdesk.ci_inquiry("check the CI and PR checks") == (True, False)
    assert main._task_inquiry_target(conv["id"], "check the CI") == task["id"]


@pytest.mark.asyncio
async def test_named_ci_questions_do_not_restart_a_shipped_worker(monkeypatch):
    conv = conversation()
    tid = shipped(conv)
    checked = []

    async def report(task_id, historical=False):
        checked.append((task_id, historical))
        return f"PR #{task_id}: green, review required"

    async def no_refine(*_a, **_k):
        raise AssertionError("read-only question restarted the code task")

    monkeypatch.setattr(tasks, "ci_report", report)
    monkeypatch.setattr(tasks, "refine", no_refine)
    monkeypatch.setattr(main, "_start_turn", lambda *_a: pytest.fail("brain turn started"))
    messages = (
        (f"Task {tid}, CI is still failing analyse the root cause", True),
        (f"Any update .?? On task {tid} CI ..?", False),
        (f"Task {tid} check thay", False),
        ("CI Passed now ,? Any reason for couple of times failure .?", True),
    )
    for text, _ in messages:
        sink = Sink()
        assert await main._dispatch(conv, text, sink, "whatsapp") is None
        assert "green, review required" in sink.text
        assert store.get_task(tid)["status"] == "shipped"
    assert checked == [(tid, past) for _, past in messages]
    assert not any(e["detail"].endswith("→ running") for e in store.task_events(tid))


@pytest.mark.asyncio
async def test_task_pr_conflict_and_unlinked_task_do_not_guess(monkeypatch):
    conv = conversation()
    tid = shipped(conv)
    actual_report = tasks.ci_report
    monkeypatch.setattr(tasks, "ci_report",
                        lambda *_a, **_k: pytest.fail("wrong PR checked"))
    sink = Sink()
    await main._dispatch(conv, f"task {tid} check CI on PR #1446", sink, "whatsapp")
    assert "/pull/1466" in sink.text and "#1446" in sink.text
    assert store.get_task(tid)["status"] == "shipped"

    monkeypatch.setattr(tasks, "ci_report", actual_report)
    other = store.create_task("unshipped", "code", "work", "booking")
    store.update_task(other["id"], status="done", finished_at=time.time())
    sink = Sink()
    await main._dispatch(conv, f"task {other['id']} check CI", sink, "whatsapp")
    assert "no linked PR" in sink.text


@pytest.mark.asyncio
async def test_explicit_edit_resumes_existing_task_not_a_new_one(monkeypatch):
    conv = conversation()
    tid = shipped(conv)
    monkeypatch.setattr(tasks, "spawn", lambda *_a, **_k: pytest.fail("new task spawned"))

    async def resumed(*_a, **_k):
        return None

    monkeypatch.setattr(tasks, "_resume_worker", resumed)
    sink = Sink()
    await main._dispatch(conv, f"Task {tid} also add a regression test", sink, "whatsapp")
    assert "continuing the open PR" in sink.text
    assert store.get_task(tid)["status"] == "running"


@pytest.mark.asyncio
async def test_task_number_does_not_turn_external_action_into_code_feedback(monkeypatch):
    conv = conversation()
    tid = shipped(conv)
    started = []
    actual_refine = tasks.refine

    def brain(_conv, text, _sink, _channel):
        started.append(text)
        return "chat turn"

    monkeypatch.setattr(main, "_start_turn", brain)
    monkeypatch.setattr(tasks, "refine", lambda *_a, **_k:
                        pytest.fail("external action resumed code"))
    text = f"task {tid} update the bug ticket status"
    assert await main._dispatch(conv, text, Sink(), "whatsapp") == "chat turn"
    assert started == [text]
    assert store.get_task(tid)["status"] == "shipped"
    monkeypatch.setattr(tasks, "refine", actual_refine)
    with pytest.raises(ValueError, match="different action"):
        await tasks.refine(tid, "update PR description")


@pytest.mark.asyncio
async def test_ci_question_does_not_edit_a_waiting_message_draft(monkeypatch):
    conv = conversation()
    tid = shipped(conv)
    draft = {"kind": "send", "what": "review it", "to": "Vinish", "channel": "teams"}
    loop.stage(conv["id"], draft)

    async def report(task_id, historical=False):
        assert task_id == tid and historical is False
        return "CI green, review required"

    monkeypatch.setattr(tasks, "ci_report", report)
    sink = Sink()
    await main._dispatch(conv, "CI status?", sink, "whatsapp")
    assert "CI green" in sink.text
    assert loop.awaiting(conv["id"]) == draft
    loop.clear_awaiting(conv["id"])


@pytest.mark.asyncio
async def test_read_only_feedback_cannot_bypass_dispatch_guard(monkeypatch):
    tid = shipped()
    with pytest.raises(ValueError, match="read-only question"):
        await tasks.refine(tid, "CI is still failing analyse the root cause")
    assert store.get_task(tid)["status"] == "shipped"


@pytest.mark.parametrize("text,want", [
    ("task 223 update me on CI", "read"),
    ("223 update on the PR?", "read"),
    ("task 223 update the test", "edit"),
    ("task 223 also cover amend", "edit"),
    ("task 223 why did the fix fail?", "read"),
    ("task 223 send the PR to Vinish", "external"),
    ("approve it once CI is green", "external"),
    ("task 223 update the PR description", "external"),
    ("task 223 update the bug ticket", "external"),
    ("task 223 add a PR comment", "external"),
    ("task 223 review the PR #1466", "external"),
    ("task 223 update the Jira transition and add PR #1466 in the comment", "external"),
    ("task 223 fix tests and send the PR to Vinish", "external"),
    ("task 223 add the PR to the ticket and tell Vinish", "external"),
    ("just update the transition and add PR in comment thats it", "external"),
    ("does jira ticket updated and pr send to vinish also for review ?", "read"),
    ("task 223 fix the PR CI", "edit"),
])
def test_task_intent_does_not_confuse_status_with_changes(text, want):
    assert frontdesk.task_intent(text) == want


def test_a_pr_reference_is_not_a_task_reference():
    assert main._explicit_task("CI status on PR #223?") is None
    assert main._named_task("CI status on PR #223?", [223]) is None
    assert main._named_task("223 also add a test", [223]) == 223


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["shipped", "running"])
async def test_jira_action_is_never_folded_into_a_code_task(monkeypatch, status):
    conv = conversation()
    tid = shipped(conv)
    store.update_task(tid, status=status)
    launched = []

    def brain(_conv, text, _sink, _channel):
        launched.append(text)
        return "chat turn"

    monkeypatch.setattr(main, "_start_turn", brain)
    monkeypatch.setattr(tasks, "refine", lambda *_a, **_k:
                        pytest.fail("Jira action restarted a code task"))
    monkeypatch.setattr(tasks, "augment", lambda *_a, **_k:
                        pytest.fail("Jira action was buffered as code feedback"))
    text = "just update the transition and add PR in comment thats it"
    assert await main._dispatch(conv, text, Sink(), "whatsapp") == "chat turn"
    assert launched == [text]
    assert store.get_task(tid)["status"] == status


@pytest.mark.asyncio
async def test_jira_delivery_question_is_not_folded_into_a_live_task(monkeypatch):
    conv = conversation()
    tid = shipped(conv)
    store.update_task(tid, status="running")
    monkeypatch.setattr(tasks, "augment", lambda *_a, **_k:
                        pytest.fail("outward-send question folded into code"))
    started = []

    def brain(_conv, text, _sink, _channel):
        started.append(text)
        return "chat turn"

    monkeypatch.setattr(main, "_start_turn", brain)
    text = "does jira ticket updated and pr send to vinish also for review ?"
    assert await main._dispatch(conv, text, Sink(), "whatsapp") == "chat turn"
    assert started == [text]


@pytest.mark.asyncio
async def test_jira_instruction_and_followup_do_not_restart_completed_work(monkeypatch):
    conv = conversation()
    tid = shipped(conv)
    monkeypatch.setattr(tasks, "refine", lambda *_a, **_k:
                        pytest.fail("Jira or Teams delivery resumed code"))
    monkeypatch.setattr(tasks, "augment", lambda *_a, **_k:
                        pytest.fail("Jira or Teams delivery became task feedback"))
    seen = []
    monkeypatch.setattr(main, "_start_turn", lambda _c, text, *_a: seen.append(text))
    for text in (
        f"task {tid}: update the Jira transition, add PR #1466 in the comment "
        "and send it to Vinish for review",
        "does Jira ticket updated and PR send to Vinish also for review?",
    ):
        await main._dispatch(conv, text, Sink(), "whatsapp")
    assert len(seen) == 2
    assert store.get_task(tid)["status"] == "shipped"
    assert store.kv_get(f"task_addenda:{tid}") in ("", None)


@pytest.mark.asyncio
async def test_named_task_does_not_swallow_jira_delivery_question(monkeypatch):
    conv = conversation()
    tid = shipped(conv)
    seen = []
    monkeypatch.setattr(main, "_start_turn", lambda _c, text, *_a: seen.append(text))
    monkeypatch.setattr(tasks, "ci_report", lambda *_a, **_k:
                        pytest.fail("Jira delivery is not a CI question"))
    text = f"task {tid}: did the Jira ticket transition and did Vinish get the PR?"
    await main._dispatch(conv, text, Sink(), "whatsapp")
    assert seen == [text]
    assert store.get_task(tid)["status"] == "shipped"


def test_generic_jira_send_cannot_stage_a_draft(monkeypatch):
    conv = conversation()
    monkeypatch.setattr(tasks, "current_conversation", lambda: conv["id"])
    result = agent.prepare_to_send("PR #1466", to="BEPTELIKOS-11174", channel="jira")
    assert "Not staged" in result and "jira_comment" in result
    assert loop.awaiting(conv["id"]) is None


def test_cli_cannot_stage_generic_jira_send():
    from fastapi.testclient import TestClient

    conv = conversation()
    main.app.dependency_overrides[main.require_auth] = lambda: None
    try:
        response = TestClient(main.app).post("/api/loop/prepare-send", json={
            "conv_id": conv["id"], "what": "PR #1466", "to": "BEPTELIKOS-11174",
            "channel": "jira"})
    finally:
        main.app.dependency_overrides.clear()
    assert response.status_code == 400
    assert loop.awaiting(conv["id"]) is None


@pytest.mark.asyncio
async def test_old_generic_jira_draft_cannot_be_sent_or_reinterpreted(monkeypatch):
    conv = conversation()
    loop.stage(conv["id"], {"kind": "send", "what": "PR #1466",
                            "to": "BEPTELIKOS-11174", "channel": "jira"})
    monkeypatch.setattr(main, "_start_turn", lambda *_a: pytest.fail("brain sent Jira"))
    monkeypatch.setattr(ops, "run", lambda *_a: pytest.fail("unapproved Jira write"))
    sink = Sink()
    await main._dispatch(conv, "send", sink, "whatsapp")
    assert "Nothing was sent" in sink.text
    assert loop.awaiting(conv["id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shown", [False, True])
async def test_newer_jira_prompt_cannot_approve_unrelated_pr_review(monkeypatch, shown):
    conv = conversation()
    tid = shipped(conv)
    token = capabilities.FROM_TASK.set("223")
    try:
        review = offers.staged_write(
            "pr_review_inline", {"pr": "94", "action": "approve", "body": "LGTM"},
            "Review PR #94", "event router review", "Post review on PR #94?",
            kind="pr_write")
    finally:
        capabilities.FROM_TASK.reset(token)
    if shown:
        review.render()
    offers.staged_write("jira_comment",
                        {"key": "BEPTELIKOS-11174", "text": "PR #1466 is ready"},
                        "Comment on BEPTELIKOS-11174", "PR #1466", "Post this comment?")
    offers.staged_write("jira_transition",
                        {"key": "BEPTELIKOS-11174", "to_status": "In Review"},
                        "Move BEPTELIKOS-11174", "In Review", "Move the ticket?")
    store.add_ui_message(conv["id"], "assistant",
                         "Ready to send to Jira — can I send this? Reply send to confirm.",
                         {"via": "loop-confirm-send"})
    monkeypatch.setattr(ops, "run", lambda *_a: pytest.fail("wrong offer approved"))
    monkeypatch.setattr(tasks, "refine", lambda *_a, **_k: pytest.fail("code reopened"))
    monkeypatch.setattr(main, "_start_turn", lambda *_a: pytest.fail("brain reinterpreted yes"))
    sink = Sink()
    await main._dispatch(conv, "yes", sink, "whatsapp")
    assert "Nothing was posted" in sink.text
    assert "Post review on PR #94?" in sink.text
    assert offers.pending().id == review.id
    assert offers.pending().shown
    assert len(offers.waiting()) == 2
    assert store.get_task(tid)["status"] == "shipped"
    sent = []

    async def run(op):
        sent.append(op)
        return "Review posted"

    monkeypatch.setattr(ops, "run", run)
    await main._dispatch(conv, "yes", Sink(), "whatsapp")
    assert sent == [review.op]
    assert offers.pending().op["name"] == "jira_comment"
    assert not offers.pending().shown


@pytest.mark.asyncio
async def test_jira_operation_only_runs_after_its_exact_offer_is_shown(monkeypatch):
    conv = conversation()
    offer = offers.staged_write(
        "jira_comment", {"key": "BEPTELIKOS-11174", "text": "PR #1466 is ready"},
        "Comment on BEPTELIKOS-11174", "PR #1466 is ready",
        "Post this comment on BEPTELIKOS-11174?")
    assert offer.is_asked()
    offer.render()
    sent = []

    async def run(op):
        sent.append(op)
        return "Comment posted"

    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(main, "_start_turn", lambda *_a: pytest.fail("brain changed Jira"))
    await main._dispatch(conv, "yes", Sink(), "whatsapp")
    assert sent == [{"name": "jira_comment",
                     "args": {"key": "BEPTELIKOS-11174", "text": "PR #1466 is ready"}}]
    assert offers.pending() is None


def test_direct_augment_rejects_outward_action():
    tid = shipped()
    with pytest.raises(ValueError, match="external action"):
        tasks.augment(tid, "just update the transition and add PR in comment")
    assert store.kv_get(f"task_addenda:{tid}") in ("", None)


@pytest.mark.asyncio
async def test_history_inspects_failed_runs_without_changing_task(monkeypatch):
    tid = shipped()
    calls = []

    async def github(_cwd, *args, **_kwargs):
        calls.append(args)
        if args[:2] == ("gh", "pr"):
            return 0, json.dumps({
                "state": "OPEN", "reviewDecision": "REVIEW_REQUIRED",
                "headRefOid": "abcdef123456", "headRefName": "fix/amend",
                "createdAt": "2026-10-05T15:00:00Z",
                "statusCheckRollup": [
                    {"name": "push / build", "conclusion": "SUCCESS"},
                    {"name": "PR / component test", "conclusion": "SUCCESS"}]})
        if args[:3] == ("gh", "run", "list"):
            return 0, json.dumps([
                {"databaseId": 9, "conclusion": "failure",
                 "workflowName": "Component test", "event": "pull_request",
                 "headSha": "old123", "createdAt": "2026-10-05T18:00:00Z",
                 "url": "https://github.com/work/booking/actions/runs/9"},
                {"databaseId": 8, "conclusion": "success",
                 "createdAt": "2026-10-05T17:00:00Z"},
                {"databaseId": 7, "conclusion": "failure",
                 "createdAt": "2026-09-01T17:00:00Z"}])
        if args[:3] == ("gh", "run", "view"):
            return 0, "[ERROR] BookingTest.testAmend -- expected validation error"
        raise AssertionError(f"unexpected GitHub call: {args}")

    monkeypatch.setattr(repo_ops, "git", github)
    current = await tasks.ci_report(tid)
    assert "CI green (2 checks); review REVIEW_REQUIRED" in current
    assert not any(args[:3] == ("gh", "run", "list") for args in calls)
    past = await tasks.ci_report(tid, historical=True)
    assert "Component test (pull_request, old123)" in past
    assert "BookingTest.testAmend" in past
    assert "actions/runs/9" in past and "actions/runs/7" not in past
    assert store.get_task(tid)["status"] == "shipped"


@pytest.mark.asyncio
async def test_unavailable_github_is_not_called_green(monkeypatch):
    tid = shipped()

    async def unavailable(*_a, **_k):
        return 1, "unavailable"

    monkeypatch.setattr(repo_ops, "git", unavailable)
    report = await tasks.ci_report(tid, historical=True)
    assert "CI is unverified" in report
    assert "CI green" not in report


@pytest.mark.asyncio
async def test_github_auth_error_is_an_explicit_unverified_result(monkeypatch):
    tid = shipped()

    async def missing_auth(*_a, **_k):
        raise RuntimeError("office account unavailable")

    monkeypatch.setattr(repo_ops, "git", missing_auth)
    report = await tasks.ci_report(tid)
    assert "authentication or network error" in report
    assert "CI is unverified" in report
    assert store.get_task(tid)["status"] == "shipped"


@pytest.mark.asyncio
async def test_concurrent_ci_requests_share_a_read_not_a_worker(monkeypatch):
    tid = shipped()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def report(_tid, _historical):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "live checks"

    monkeypatch.setattr(tasks, "_ci_report", report)
    first = asyncio.create_task(tasks.ci_report(tid))
    await entered.wait()
    second = asyncio.create_task(tasks.ci_report(tid))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == ["live checks", "live checks"]
    assert calls == 1
    assert store.get_task(tid)["status"] == "shipped"


def test_authenticated_ci_endpoint_is_read_only(monkeypatch):
    from fastapi.testclient import TestClient

    tid = shipped()

    async def report(task_id, historical=False):
        assert task_id == tid and historical is True
        return "earlier run failed; current checks green"

    monkeypatch.setattr(tasks, "ci_report", report)
    main.app.dependency_overrides[main.require_auth] = lambda: None
    try:
        response = TestClient(main.app).get(f"/api/tasks/{tid}/ci?historical=true")
    finally:
        main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "earlier run failed" in response.json()["report"]
    assert store.get_task(tid)["status"] == "shipped"


#: His message on 7 Oct, word for word: answered with #257's status card.
DOMAIN_FEEDBACK = ("all the feedbacks workprocess comes from billing are FINANCE_MILESTONE , "
                   "like how we keeping for job closure , rememember for future use , "
                   "now update it correctly")


def test_domain_feedback_ending_in_update_it_is_an_edit():
    assert frontdesk.task_intent(f"task 257 - {DOMAIN_FEEDBACK}") == "edit"
    assert frontdesk.task_intent("task 257 any update?") == "read"
    assert frontdesk.task_intent("task 257 update me on CI") == "read"


@pytest.mark.asyncio
async def test_feedback_reaches_a_running_task_and_is_remembered(monkeypatch):
    from app import memory
    conv = conversation()
    task = store.create_task("Plan booking-service implementation of AP PR 1260", "code",
                             "plan", "booking")
    store.update_task(task["id"], status="running")
    kept = []
    monkeypatch.setattr(memory, "remember", lambda title, fact, mtype="fact": kept.append(fact))
    monkeypatch.setattr(tasks, "ci_report", lambda *_a, **_k: pytest.fail("read as a status ask"))
    sink = Sink()
    await main._dispatch(conv, f"task {task['id']} - {DOMAIN_FEEDBACK}", sink, "whatsapp")
    assert "FINANCE_MILESTONE" in (store.kv_get(f"task_addenda:{task['id']}") or "")
    assert "Saved it for future work" in sink.text
    assert kept and "FINANCE_MILESTONE" in kept[0] and "rememember" not in kept[0]


@pytest.mark.asyncio
async def test_feedback_on_shipped_work_resumes_it(monkeypatch):
    from app import memory
    conv = conversation()
    tid = shipped(conv)
    monkeypatch.setattr(memory, "remember", lambda *a, **k: "")

    async def resumed(*_a, **_k):
        return None
    monkeypatch.setattr(tasks, "_resume_worker", resumed)
    sink = Sink()
    await main._dispatch(conv, f"task {tid} - {DOMAIN_FEEDBACK}", sink, "whatsapp")
    assert "continuing the open PR" in sink.text
    assert store.get_task(tid)["status"] == "running"
