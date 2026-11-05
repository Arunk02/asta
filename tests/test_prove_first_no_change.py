"""A prove-first code task that proves nothing is wrong ends there (5 Nov).

Synthetic: a task told to "prove it before you fix it" traced the field end to
end, found it in the published payload, and had no outcome that said so — it
reported `escalate`, was rerun through the full pipeline, and came back as a
question. The task and service names are made up.
"""
from __future__ import annotations

import asyncio

import pytest

from app import store
from app.graph import code_graph, outcome


def _task(pipeline="micro"):
    t = store.create_task("Fix the dropped date", "code", "prove it before fix", "ws")
    store.kv_set(f"task_pipeline:{t['id']}", pipeline)
    return t["id"]


def test_no_change_is_a_kind_a_run_can_report():
    tid = _task()
    assert "no_change" in outcome.record(tid, "no_change", "present in the payload")
    assert "no_change" in outcome.rider(tid)


@pytest.mark.parametrize("out", [
    {"kind": "no_change", "summary": "VESSEL date present in the live payload"},
    {"kind": "escalate", "summary": "Traced it end to end. No drop found in code or in "
                                     "the live payload. No code changes made."},
])
def test_a_proved_no_defect_ends_instead_of_escalating(out):
    assert code_graph.after_plan({"task_id": _task(), "outcome": out}) == "no_change"


def test_a_real_escalation_still_escalates():
    out = {"kind": "escalate", "summary": "touches three services and a schema"}
    assert code_graph.after_plan({"task_id": _task(), "outcome": out}) == "escalate"


def test_no_change_completes_the_task_with_its_proof(monkeypatch):
    tid = _task()
    seen = {}

    async def complete(task_id, t, result):
        seen.update(id=task_id, result=result)
    monkeypatch.setattr(code_graph._tasks(), "complete", complete)
    asyncio.run(code_graph.no_change({"task_id": tid, "outcome": {
        "kind": "no_change", "summary": "present at svc:Mapper.java:244, log 14:08:42Z"}}))
    assert seen["id"] == tid
    assert seen["result"].startswith("No change needed") and "Mapper.java:244" in seen["result"]


def test_the_graph_has_the_route():
    g = code_graph.build().compile()
    assert "no_change" in g.get_graph().nodes


def test_a_question_too_long_for_the_phone_starts_at_its_first_word():
    from app import tasks
    para = ("Live check of the order shows the payload DOES carry the date. " * 30).strip()
    out = tasks._phone_text(para, 300)
    assert out.startswith("Live check of the order") and out.endswith("…")
    assert len(out) <= 300
