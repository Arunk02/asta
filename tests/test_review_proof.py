"""A review reads what it needs and posts only what it can prove (12 Nov).

Shapes from the live history — a blocker about a plan shape the flow never
produces, Copilot's points left unchecked, seven comments on one PR — with
synthetic repos, paths and bookings only.
"""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from app import offers, review, review_outcomes, review_proof, store

pytestmark = pytest.mark.real_proof_check

LOGIC = {"number": 7, "title": "Price date for one-way empty orders", "body": "fails in sit for HABCDEFGH12",
         "url": "https://github.com/acme/booking/pull/7", "headRefOid": "a" * 40,
         "headRepository": {"name": "booking"},
         "files": [{"path": "service/src/main/java/acme/DomainHelper.java"},
                   {"path": "service/src/test/java/acme/DomainHelperTest.java"}],
         "diff": "@@ -300,6 +300,12 @@ public static String fetchPriceDate(ServicePlan plan) {\n"
                 "+  private static Optional<String> findOneWayEmptyDate(Leg leg) {\n"}
LIGHT = {**LOGIC, "files": [{"path": "service/src/test/java/acme/XTest.java"},
                            {"path": "service/src/main/resources/application.yml"}]}


def _f(path, line, body, blocking=False):
    return {"path": path, "line": line, "body": body, "blocking": blocking, "proof": ""}


# --- what the review reads, sized to the change ----------------------------------------

def test_tests_and_config_are_a_light_review_and_logic_is_not():
    assert review_proof.size_of(LIGHT) == "light"
    assert review_proof.size_of(LOGIC) == "logic"


def test_changed_methods_come_from_hunks_and_new_declarations():
    assert review_proof.changed_methods(LOGIC["diff"]) == ["fetchPriceDate", "findOneWayEmptyDate"]


def test_a_light_change_reads_only_its_diff(monkeypatch):
    out = asyncio.run(review_proof.needs(LIGHT, "ws", None))
    assert out.startswith("SIZE: light") and "CALLERS" not in out and "DOMAIN" not in out


def test_a_logic_change_gets_callers_docs_and_earlier_disputes(tmp_path, monkeypatch):
    repo = tmp_path / "booking"
    (repo / "src/main/java").mkdir(parents=True)
    (repo / "src/main/java/Caller.java").write_text(
        "class Caller {\n  void run() {\n    String d = DomainHelper.fetchPriceDate(plan);\n  }\n}\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    monkeypatch.setattr(review_proof, "domain_docs", lambda m, w, n, budget=3000: "One-way empty: one leg.")
    review_outcomes._add_dispute("booking", {"path": "DomainHelper.java", "body": "leg[0] only",
                                             "reply": "one-way empty always has one leg"})
    out = asyncio.run(review_proof.needs(LOGIC, "ws", repo))
    assert "SIZE: logic" in out
    assert "Caller.java:3" in out, "the caller of the changed method is named"
    assert "One-way empty: one leg." in out
    assert "always has one leg" in out, "a dispute the author won is not raised again"


# --- proven stays, unproven is asked, wrong is dropped ----------------------------------

FINDINGS = [_f("a/DomainHelper.java", 336, "only leg[0] is checked", blocking=True),
            _f("a/DomainHelper.java", 355, "first in list order, not earliest"),
            _f("a/Consumer.java", 69, "early return is untested", blocking=True)]


def test_each_verdict_is_applied_and_the_merge_is_not_blocked_by_a_guess():
    verdicts = {1: {"verdict": "unproven", "proof": "no multi-leg one-way empty plan exists"},
                2: {"verdict": "wrong", "proof": "a leg carries one CARGO_UNLOAD_START"},
                3: {"verdict": "proven", "proof": "no test calls the null path"}}
    kept, action, report = review_proof.apply(FINDINGS, verdicts, "request_changes")
    assert [k["line"] for k in kept] == [69, 336]
    assert kept[0]["blocking"] and kept[0]["proof"] == "no test calls the null path"
    assert kept[1]["body"].startswith("Question: ") and not kept[1]["blocking"]
    assert action == "request_changes", "one proven blocker still blocks"
    assert any(r.startswith("✗ DomainHelper.java:355 — dropped") for r in report)


def test_without_a_proven_blocker_changes_are_not_requested():
    verdicts = {1: {"verdict": "unproven", "proof": "x"}, 2: {"verdict": "wrong", "proof": "y"},
                3: {"verdict": "unproven", "proof": "z"}}
    kept, action, _ = review_proof.apply(FINDINGS, verdicts, "request_changes")
    assert action == "comment" and all(k["body"].startswith("Question: ") for k in kept)


def test_nothing_unchecked_is_posted_as_a_finding():
    kept, action, report = review_proof.apply(FINDINGS, {}, "request_changes")
    assert all(k["body"].startswith("Question: ") and not k["blocking"] for k in kept)
    assert action == "comment"


def test_at_most_three_lesser_points_reach_the_pr():
    many = [_f("a/X.java", i, f"point {i}") for i in range(1, 8)]
    verdicts = {i: {"verdict": "proven", "proof": "p"} for i in range(1, 8)}
    kept, _, report = review_proof.apply(many, verdicts, "comment")
    assert len(kept) == review_proof.MAX_OTHER_COMMENTS and "left out" in report[-1]


def test_the_verdicts_are_read_from_a_chatty_reply():
    said = 'Checked all three.\n```json\n[{"n": 1, "verdict": "Proven", "proof": "caller A:9"},' \
           ' {"n": 9, "verdict": "proven"}, {"n": 2, "verdict": "maybe"}]\n```'
    assert review_proof._verdicts(said, 3) == {1: {"verdict": "proven", "proof": "caller A:9"}}


def test_the_check_runs_on_the_stronger_model_read_only(monkeypatch):
    from app import claude_cli
    seen = {}

    async def one_shot(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return json.dumps([{"n": 1, "verdict": "wrong", "proof": "the flow has one leg"},
                           {"n": 2, "verdict": "proven", "proof": "test missing"}])

    async def code(meta, findings, around=60):
        return "### a/DomainHelper.java\n336: legs.stream().findFirst()"

    async def data(meta):
        return "HABCDEFGH12 in sit:\norder type ONE_WAY_EMPTY · 1 leg(s)"
    monkeypatch.setattr(claude_cli, "one_shot", one_shot)
    monkeypatch.setattr(review_proof, "_code_at_head", code)
    monkeypatch.setattr(review_proof, "real_input", data)
    monkeypatch.setattr(review_proof, "domain_docs", lambda *a, **k: "")
    kept, action, report = asyncio.run(review_proof.check(
        [FINDINGS[0], FINDINGS[2]], "request_changes", LOGIC, "ws", None))
    assert seen["model"] == review_proof.VERIFY_MODEL == "opus" and seen["plan_only"] is True
    assert "ONE_WAY_EMPTY · 1 leg(s)" in seen["prompt"] and "NOT proof" in seen["prompt"]
    assert [k["line"] for k in kept] == [69] and action == "request_changes"
    assert report[0].startswith("real data read: HABCDEFGH12")


def test_when_the_check_cannot_run_every_point_is_a_question(monkeypatch):
    from app import claude_cli

    async def broken(prompt, **kw):
        raise RuntimeError("quota")

    async def nothing(*a, **k):
        return ""
    monkeypatch.setattr(claude_cli, "one_shot", broken)
    monkeypatch.setattr(review_proof, "_code_at_head", nothing)
    monkeypatch.setattr(review_proof, "real_input", nothing)
    monkeypatch.setattr(review_proof, "domain_docs", lambda *a, **k: "")
    kept, action, report = asyncio.run(review_proof.check(FINDINGS, "request_changes", LOGIC, "ws", None))
    assert action == "comment" and all(k["body"].startswith("Question: ") for k in kept)
    assert "could not run" in report[0]


# --- real data: the booking the PR names ------------------------------------------------

def test_the_plan_shape_is_read_from_a_payload():
    payload = {"booking": {"orderType": "ONE_WAY_EMPTY", "servicePlanLegs": [
        {"startLocation": {"locationFunction": "PLACE_OF_DELIVERY"},
         "endLocation": {"locationFunction": "PLACE_OF_EMPTY_EQUIPMENT_DROP"},
         "serviceDates": [{"eventTrigger": "CARGO_UNLOAD_START"}, {"eventTrigger": "EMPTY_RETURN"}]}]}}
    shape = review_proof.plan_shape(payload)
    assert "order type ONE_WAY_EMPTY · 1 leg(s)" in shape
    assert "PLACE_OF_DELIVERY → PLACE_OF_EMPTY_EQUIPMENT_DROP · dates: CARGO_UNLOAD_START, EMPTY_RETURN" in shape


def test_no_booking_or_no_env_named_reads_nothing(monkeypatch):
    from app import temporal
    monkeypatch.setattr(temporal, "enabled", lambda: True)
    assert asyncio.run(review_proof.real_input({**LOGIC, "body": "fixes the date"})) == ""


# --- at the staging seam ----------------------------------------------------------------

NOTES = """VERDICT: REQUEST CHANGES — leg[0] only.
BLOCKING
- `a/DomainHelper.java:336` — only leg[0] is checked · proof: nothing restricts it
NON-BLOCKING
- `a/DomainHelper.java:355` — first in list order
"""


def _stage(monkeypatch, verdicts):
    async def gather(pr, ws="", repo=""):
        return LOGIC

    async def revision(pr):
        return "acme/booking#7@" + "a" * 40 + ":f", True

    async def one_shot(prompt, **kw):
        return json.dumps(verdicts)

    async def nothing(*a, **k):
        return ""
    from app import agent, claude_cli
    monkeypatch.setattr(review, "gather", gather)
    monkeypatch.setattr(review, "revision", revision)
    monkeypatch.setattr(claude_cli, "one_shot", one_shot)
    monkeypatch.setattr(review_proof, "_code_at_head", nothing)
    monkeypatch.setattr(review_proof, "real_input", nothing)
    monkeypatch.setattr(review_proof, "domain_docs", lambda *a, **k: "")
    return asyncio.run(agent.propose_pr_review("https://github.com/acme/booking/pull/7", NOTES))


def test_a_disproved_blocker_never_reaches_the_staged_review(monkeypatch):
    said = _stage(monkeypatch, [{"n": 1, "verdict": "wrong", "proof": "the plan has one leg"},
                                {"n": 2, "verdict": "unproven", "proof": "one date per leg"}])
    o = offers.pending()
    assert "Staged a comment review" in said, "no proven blocker: changes are not requested"
    comments = o.op["args"]["comments"]
    assert len(comments) == 1 and comments[0]["body"].startswith("Question: ")
    assert o.op["args"]["action"] == "comment" and o.op["args"]["body"].startswith("VERDICT: COMMENT")
    assert "✗ DomainHelper.java:336 — dropped: the plan has one leg" in o.context


def test_all_points_disproved_stages_nothing(monkeypatch):
    said = _stage(monkeypatch, [{"n": 1, "verdict": "wrong", "proof": "one leg"},
                                {"n": 2, "verdict": "wrong", "proof": "one date"}])
    assert said.startswith("Not staged: every point was disproved") and offers.pending() is None


def test_another_reviewers_wrong_point_is_shown_but_never_posted():
    found = review.parse_findings(
        "BLOCKING\n- `a/X.java:9` — null deref on retry · proof: Retry.java:40 passes null\n"
        "NOT AN ISSUE\n- `a/X.java:20` — Copilot: unbounded list → capped at 50 on line 18\n")
    assert [f["line"] for f in found] == [9] and found[0]["proof"] == "Retry.java:40 passes null"
    assert "NOT AN ISSUE" in review.REVIEW_BRIEF and "{needs}" in review.REVIEW_BRIEF


# --- what authors did with what was posted ----------------------------------------------

def test_a_later_commit_on_the_commented_lines_counts_as_fixed():
    assert review_outcomes.touched("@@ -66,7 +66,9 @@ class C\n", 69)
    assert not review_outcomes.touched("@@ -120,3 +120,4 @@\n", 69)


def test_an_author_reply_that_it_cannot_happen_is_a_dispute():
    assert review_outcomes.is_dispute("Bro this case is not possible, one-way empty has one leg")
    assert review_outcomes.is_dispute("can't happen here")
    assert not review_outcomes.is_dispute("good catch, fixed")


def test_outcomes_are_read_back_and_scored(monkeypatch):
    review_outcomes.record_posted("acme/booking", "7", "a" * 40, [
        {"path": "a/H.java", "line": 336, "body": "only leg[0] is checked"},
        {"path": "a/C.java", "line": 69, "body": "early return is untested"}])
    calls = []

    async def gh(cwd, *args, timeout=60):
        calls.append(args)
        path = args[1] if args[0] == "api" and not args[1].startswith("--") else args[2]
        if path.endswith("/pulls/7"):
            return 0, "b" * 40 + "\tOPEN"
        if "/compare/" in path:
            return 0, json.dumps({"filename": "a/C.java", "patch": "@@ -66,7 +66,9 @@\n"})
        if path.endswith("/comments"):
            return 0, "\n".join(json.dumps(r) for r in [
                {"id": 1, "in_reply_to_id": None, "path": "a/H.java", "line": 336,
                 "body": "only leg[0] is checked", "login": "me"},
                {"id": 2, "in_reply_to_id": 1, "path": "a/H.java", "line": 336,
                 "body": "not possible, one leg only", "login": "author"}])
        return 1, ""

    async def mine():
        return {"me"}
    monkeypatch.setattr(review, "_gh", gh)
    monkeypatch.setattr(review, "my_logins", mine)
    assert asyncio.run(review_outcomes.sweep()) == 1
    s = review_outcomes.score()
    assert s == {"accepted": 1, "disputed": 1, "pending": 0}
    assert "1 fixed by the author · 1 disputed" in review_outcomes.score_line()
    assert review_outcomes.disputes("booking")[-1]["reply"] == "not possible, one leg only"
    assert asyncio.run(review_outcomes.sweep()) == 0, "read again only after a while"


def test_the_judge_drops_a_point_the_real_data_contradicts_and_a_non_defect():
    """Replay of the 1538 shape: the real booking had one leg; a 'consistent with
    existing code' note is not a finding."""
    brief = review_proof.VERIFY_BRIEF
    assert "the booking has one\n  leg; the comment needs several" in brief
    assert "matches how the\n  codebase already does the same thing" in brief
