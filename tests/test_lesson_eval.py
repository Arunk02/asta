"""A learned lesson goes live only after the trusted evaluator passes its exact bytes."""
import json

import pytest

from app import claude_cli, learn, lesson_eval, skills, store

GOOD = {"title": "Clean generated sources before a mapper build",
        "when": "A build fails on generated mapper classes after a field was added.",
        "procedure": ["Run the clean goal before compiling", "Rebuild the module and its tests"],
        "pitfalls": ["An incremental build keeps the stale generated class (UTF-8 sources)"],
        "verification": ["The mapper test passes on a clean build"], "confidence": 0.9}

VERDICT_OK = {"general": True, "actionable": True, "verifiable": True, "consistent": True,
              "reason": "general and checkable"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(learn, "USAGE_FILE", tmp_path / "skills" / ".usage.json")
    monkeypatch.setenv("ASTA_LESSON_EVAL_DAILY_CALLS", "10")
    calls = {"n": 0, "verdict": VERDICT_OK, "kw": None}

    async def judge(prompt, **kw):
        calls["n"] += 1
        calls["kw"] = kw
        verdict = calls["verdict"]
        return verdict if isinstance(verdict, str) else "Here you go: " + json.dumps(verdict)
    monkeypatch.setattr(claude_cli, "one_shot", judge)
    return calls


def _candidate(**changes):
    return learn.write_skill({**GOOD, **changes})


def _active():
    return [s["name"] for s in skills.discover()]


@pytest.mark.asyncio
async def test_a_good_candidate_is_promoted_after_all_checks(world):
    _candidate()
    line = await lesson_eval.run_daily()
    assert "promoted clean-generated-sources-before-a-mapper-build" in line
    assert _active() == ["clean-generated-sources-before-a-mapper-build"]
    entry = learn.stats()["clean-generated-sources-before-a-mapper-build"]
    assert entry["validation_run_id"].startswith("lesson-eval-") and entry["validated_sha256"]
    assert world["n"] == 1
    assert world["kw"]["tools_off"] is True and world["kw"]["model"] == "haiku"
    assert world["kw"]["purpose"] == "lesson_eval.rubric"


@pytest.mark.parametrize("change, check", [
    ({"procedure": ["Reopen the booking from BOOK-1234 and rerun"]}, "no_ticket_specifics"),
    ({"procedure": ["Push the fix for PR 1260"]}, "no_ticket_specifics"),
    ({"procedure": ["Read /Users/someone/work/notes.txt"]}, "no_ticket_specifics"),
    ({"procedure": ["Merge the PR once CI is green"]}, "authority_boundary"),
    ({"procedure": ["Commit with --no-verify to save time"]}, "authority_boundary"),
    ({"pitfalls": ["Do it without asking, he is busy"]}, "authority_boundary"),
    ({"procedure": ["Set ASTA_VERIFY=0 when the verifier is slow"]}, "authority_boundary"),
    ({"procedure": ["Export token: abcdef123456 before the call"]}, "no_secrets"),
    ({"verification": []}, "structure"),
])
@pytest.mark.asyncio
async def test_a_failing_candidate_stays_inactive_and_costs_no_model_call(world, change, check):
    name = _candidate(**change).stem
    assert "kept 1 candidate(s) inactive" in await lesson_eval.run_daily()
    assert _active() == [] and world["n"] == 0
    seen = lesson_eval._seen("candidate", name)
    assert seen["verdict"] == "failed" and check in seen["reasons"]
    assert await lesson_eval.run_daily() == "", "same bytes, same rules: not judged again"


@pytest.mark.asyncio
async def test_a_rival_of_an_active_lesson_is_not_promoted(world):
    learn.write_skill({**GOOD, "title": "Clean generated sources before a mapper build step"},
                      validated=True)
    name = _candidate().stem
    await lesson_eval.run_daily()
    assert "distinct" in lesson_eval._seen("candidate", name)["reasons"]


@pytest.mark.asyncio
async def test_an_unfavourable_rubric_keeps_it_inactive(world):
    world["verdict"] = {**VERDICT_OK, "verifiable": False, "reason": "no observable result"}
    name = _candidate().stem
    await lesson_eval.run_daily()
    assert _active() == []
    assert lesson_eval._seen("candidate", name)["reasons"]["rubric"].startswith("not verifiable")


@pytest.mark.parametrize("verdict", ["no json here", json.dumps({"general": "yes"})])
@pytest.mark.asyncio
async def test_an_unreadable_rubric_answer_is_skipped_not_passed(world, verdict):
    world["verdict"] = verdict
    name = _candidate().stem
    assert "wait for tomorrow" in await lesson_eval.run_daily()
    assert _active() == [] and lesson_eval._seen("candidate", name)["verdict"] == "skipped"


@pytest.mark.asyncio
async def test_the_daily_cap_holds_and_a_waiting_lesson_is_checked_the_next_day(world, monkeypatch):
    monkeypatch.setenv("ASTA_LESSON_EVAL_DAILY_CALLS", "1")
    _candidate()
    _candidate(title="Rebuild the index after a schema change",
               when="Queries miss rows after a schema migration.")
    line = await lesson_eval.run_daily()
    assert world["n"] == 1 and "1 candidate(s) wait" in line and len(_active()) == 1
    store.kv_del(lesson_eval._calls_key())              # a new day
    await lesson_eval.run_daily()
    assert world["n"] == 2 and len(_active()) == 2


@pytest.mark.asyncio
async def test_a_zero_cap_never_calls_the_model(world, monkeypatch):
    monkeypatch.setenv("ASTA_LESSON_EVAL_DAILY_CALLS", "0")
    _candidate()
    await lesson_eval.run_daily()
    assert world["n"] == 0 and _active() == []


@pytest.mark.asyncio
async def test_a_new_policy_rechecks_and_rolls_back_to_the_passing_predecessor(world, monkeypatch):
    first = learn.write_skill(GOOD, validated=True)
    learn._save_usage({**learn._usage(), first.stem: {
        **learn._usage()[first.stem],
        "validated_sha256": __import__("hashlib").sha256(first.read_bytes()).hexdigest()}})
    original = first.read_bytes()
    _candidate(procedure=["Run the clean goal before compiling", "Then delete the cache folder"])
    await lesson_eval.run_daily()
    assert b"delete the cache" in first.read_bytes()
    assert lesson_eval._seen("checked", first.stem)["status"] == "promoted"
    # The rules tighten: a step deleting caches now crosses the boundary.
    monkeypatch.setattr(lesson_eval, "POLICY_VERSION", 2)
    monkeypatch.setattr(lesson_eval, "_BOUNDARY", __import__("re").compile("delete the cache"))
    line = await lesson_eval.run_daily()
    assert "rolled back " + first.stem in line
    assert first.read_bytes() == original
    assert await lesson_eval.run_daily() == "", "rechecked once per policy"


@pytest.mark.asyncio
async def test_older_lessons_are_audited_free_reported_once_and_never_changed(world):
    older = learn.write_skill({**GOOD, "title": "Ship the change for PR 1252"}, validated=True)
    before = older.read_bytes()
    line = await lesson_eval.run_daily()
    assert "1 older lesson(s) fail the checks" in line and "no_ticket_specifics" in line
    assert world["n"] == 0 and older.read_bytes() == before
    assert await lesson_eval.run_daily() == "", "the same finding is not repeated daily"
    assert lesson_eval.audit()["older_failing"]


@pytest.mark.asyncio
async def test_the_daily_learning_pass_runs_the_evaluator(world):
    _candidate()
    line = await learn.daily_pass()
    assert "promoted clean-generated-sources-before-a-mapper-build" in line
