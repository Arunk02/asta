"""Hazards are caught before a reviewer sees them; review comments are answered by
Asta (17 Nov). Synthetic repos, logins and PRs only.

A booking-service PR's first push looked references up through Set.of(...).contains()
— a NullPointerException on any reference with no type — and a colleague found
it in review. His follow-up "take the comments and fix it" took four messages.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import agent, hazards, repo_ops, review_autofix, store, tasks

PR = "https://github.com/acme/booking/pull/77"


def _diff(path: str, added: str = "", removed: str = "") -> str:
    plus = "".join(f"+{l}\n" for l in added.splitlines())
    minus = "".join(f"-{l}\n" for l in removed.splitlines())
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{minus}{plus}"


NPE_V1 = _diff("svc/src/main/java/a/OceanMapper.java", """\
  private static final Set<String> OCEAN_TYPES =
      Set.of("OCEAN_CARRIER", "OCEAN_SCAC_CODE");
    keys.stream().filter(OCEAN_TYPES::contains).collect(Collectors.toSet());""")


def test_the_17nov_null_lookup_is_found():
    found = hazards.scan(NPE_V1)
    assert any("OCEAN_TYPES" in h and "NullPointerException" in h for h in found)


def test_the_guarded_version_is_clean():
    assert hazards.scan(NPE_V1 + "+    .filter(Objects::nonNull)\n") == []


@pytest.mark.parametrize("diff, needle", [
    (_diff("a/Mapper.java", "Map<String, R> m = refs.stream().collect(Collectors.toMap(R::key, R::value));"),
     "Collectors.toMap"),
    (_diff("a/Mapper.java", "if (ref.getType().equals(other)) {"), "Objects.equals"),
    (_diff("a/Api.java", "", "  public Booking findBooking(String id) {"), "findBooking"),
    (_diff("a/Booking.avsc", '{"name": "bookingNumber", "type": "string"}',
           '{"name": "bookingNo", "type": "string"}'), "bookingNo"),
    (_diff("a/StatusEnum.java", "public enum StatusEnum {\n  OPEN,", "  CLOSED_LATE,"), "CLOSED_LATE"),
    (_diff("a/BookingWorkflowImpl.java", "Workflow.sleep(Duration.ofMinutes(5));"), "getVersion"),
])
def test_each_class_of_hazard_is_named(diff, needle):
    assert any(needle in h for h in hazards.scan(diff)), hazards.scan(diff)


def test_tests_are_not_scanned():
    assert hazards.scan(_diff("svc/src/test/java/a/OceanMapperTest.java",
                              "Set<String> s = Set.of(\"A\");\nassertTrue(s.contains(x));")) == []


@pytest.mark.parametrize("name", ["solo", "micro"])
def test_every_code_pipeline_carries_the_checklist(name):
    from app import agents
    body = agents.load(name)
    for words in ("Before you commit", "Guard every lookup", "keeping the old one working",
                  "versioning", "behaviour you did NOT mean to change", "Zero tests run"):
        assert words in body, words


def _pr(author):
    return {"author": {"login": author}} if author else {}


@pytest.mark.parametrize("author, his", [("Dev-Owner", True), ("someone-else", False),
                                          ("", False)])
def test_only_his_own_pr_is_handled_automatically(monkeypatch, author, his):
    from app import ci_watch

    async def me():
        return "dev-owner"

    monkeypatch.setattr(ci_watch, "my_login", me)
    assert asyncio.run(review_autofix.his_change(_pr(author))) is his


def test_no_login_means_nothing_automatic(monkeypatch):
    from app import ci_watch

    async def me():
        return ""

    monkeypatch.setattr(ci_watch, "my_login", me)
    assert asyncio.run(review_autofix.his_change(_pr("dev-owner"))) is False


def test_a_verdict_is_all_or_nothing():
    two = '[{"n": 1, "valid": true, "why": "Mapper.java:40 NPE"}, {"n": 2, "valid": false, "why": "x"}]'
    assert review_autofix.parse_verdicts(two, 2) == [
        {"valid": True, "why": "Mapper.java:40 NPE"}, {"valid": False, "why": "x"}]
    assert review_autofix.parse_verdicts(two, 3) is None, "half a verdict is no verdict"
    assert review_autofix.parse_verdicts("not json", 1) is None


@pytest.fixture
def code_task():
    t = store.create_task("Ocean EH diff", "code", "fix it", "booking")
    store.update_task(t["id"], status="shipped", pr_urls=PR)
    return t["id"]


@pytest.fixture
def gh(monkeypatch):
    calls: list[tuple] = []

    async def git(cwd, *args, **kw):
        calls.append(args)
        if args[:3] == ("gh", "pr", "diff"):
            return 0, "diff --git a/x b/x\n+code\n"
        return 0, ""

    monkeypatch.setattr(repo_ops, "git", git)
    return calls


NOTES = ["reviewer1 (svc/OceanMapper.java:40) [c901]: Set.of contains(null) throws NPE",
         "reviewer1: rename the constant to OCEAN_REFERENCE_TYPES"]


def test_valid_comments_go_to_the_same_task_with_the_plan_approved(monkeypatch, code_task, gh):
    async def judge(prompt, timeout=120):
        return ('[{"n": 1, "valid": true, "why": "OceanMapper.java:40 null lookup"},'
                ' {"n": 2, "valid": false, "why": "name matches the codebase convention"}]')

    sent = {}

    async def refine(task_id, feedback, *, code_change=False, approved_plan=False):
        sent.update(task_id=task_id, feedback=feedback, code_change=code_change,
                    approved=approved_plan)
        return "ok"

    monkeypatch.setattr(agent, "one_shot_any", judge)
    monkeypatch.setattr(tasks, "refine", refine)
    line = asyncio.run(review_autofix.handle(code_task, PR, NOTES))
    assert sent["task_id"] == code_task and sent["approved"] and sent["code_change"]
    assert "Set.of contains(null)" in sent["feedback"]
    assert "rename the constant" not in sent["feedback"], "a not-valid comment is not fixed"
    assert "fixing 1 valid" in line
    assert review_autofix.pending(code_task)["url"] == PR


def test_nothing_valid_changes_nothing_and_argues_with_nobody(monkeypatch, code_task, gh):
    async def judge(prompt, timeout=120):
        return '[{"n": 1, "valid": false, "why": "already guarded at OceanMapper.java:38"}]'

    async def refine(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr(agent, "one_shot_any", judge)
    monkeypatch.setattr(tasks, "refine", refine)
    line = asyncio.run(review_autofix.handle(code_task, PR, NOTES[:1]))
    assert "Not replied on the PR" in line
    assert review_autofix.pending(code_task) is None
    assert not any(a[:2] == ("gh", "api") and "replies" in " ".join(a) for a in gh)


def test_after_the_push_each_fixed_comment_is_answered_on_its_thread(code_task, gh):
    store.kv_set(f"review_autofix:{code_task}", json.dumps({"url": PR, "items": [
        {"note": NOTES[0], "valid": True, "why": "null lookup guarded"},
        {"note": "reviewer2: also add a test for the export side", "valid": True,
         "why": "test added"},
        {"note": NOTES[1], "valid": False, "why": "convention"}]}))
    posted = asyncio.run(review_autofix.after_push(code_task, "abc1234def99"))
    assert posted == 2
    thread = [a for a in gh if a[:2] == ("gh", "api")]
    assert thread and "repos/acme/booking/pulls/77/comments/901/replies" in thread[0]
    assert any("body=Fixed in abc1234def: null lookup guarded" == x for x in thread[0])
    general = [a for a in gh if a[:3] == ("gh", "pr", "comment")]
    assert general and "@reviewer2 Fixed in abc1234def: test added" in general[0][-1]
    assert not any("convention" in " ".join(a) for a in gh), "not-valid stays his"
    assert review_autofix.pending(code_task) is None


def test_the_fourth_round_is_his(monkeypatch, code_task, gh):
    store.kv_set(f"review_autofix_rounds:{code_task}", str(review_autofix.MAX_ROUNDS))

    async def judge(prompt, timeout=120):
        raise AssertionError("no judging past the limit")

    monkeypatch.setattr(agent, "one_shot_any", judge)
    line = asyncio.run(review_autofix.handle(code_task, PR, NOTES[:1]))
    assert "this one is yours" in line


def test_hazards_found_in_its_own_diff_are_fixed_on_the_pr_once(monkeypatch, code_task):
    store.kv_set(f"task_hazards:{code_task}", json.dumps(["OceanMapper.java: Set.of null lookup"]))
    briefs = []

    async def refine(task_id, feedback, *, code_change=False, approved_plan=False):
        briefs.append((feedback, approved_plan))
        return "ok"

    monkeypatch.setattr(tasks, "refine", refine)
    line = asyncio.run(review_autofix.fix_hazards(code_task, PR))
    assert briefs and "Set.of null lookup" in briefs[0][0] and briefs[0][1]
    assert "before review" in line
    assert store.kv_get(f"hazard_round:{code_task}") == "1"
    assert review_autofix.pending(code_task) == {"url": PR, "items": []}


def test_line_comments_are_read_and_replies_in_threads_skipped(monkeypatch):
    rows = [{"id": 5, "user": {"login": "Reviewer1"}, "body": "null here", "path": "a/M.java",
             "line": 12},
            {"id": 6, "user": {"login": "reviewer1"}, "body": "and here", "path": "a/M.java",
             "line": 13, "in_reply_to_id": 5},
            {"id": 7, "user": {"login": "sonarqubecloud[bot]"}, "body": "gate"},
            {"id": 8, "user": {"login": "me-login"}, "body": "my own reply", "path": "a/M.java"}]

    async def git(cwd, *args, **kw):
        return 0, json.dumps(rows)

    monkeypatch.setattr(repo_ops, "git", git)
    notes = asyncio.run(review_autofix.inline_notes(PR, me="me-login"))
    assert notes == ["reviewer1 (a/M.java:12) [c5]: null here"]
