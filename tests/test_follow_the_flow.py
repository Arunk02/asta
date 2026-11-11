"""A booking is debugged by walking its flow to the first step that did not
complete — the flow read from the workspace's own documents (11 Nov).

"Check X, Y" then "Fine na??" was answered at 10:28 "consumed and acked clean,
no errors": the ids were in the earlier line so no case brief was built, the
step that failed an hour later (a business exception logged at INFO) was never
looked for, and the same answer was shown again at 11:35. Synthetic ids, people
and messages throughout.
"""
from __future__ import annotations

import asyncio
import json
import time

from app import answers, booking_case, grafana, responder, store, tasks

CASE = "MX4TESTAB12C"
OTHER = "MX4TESTZZ99Q"


def _rec(ts, svc, line):
    return {"timestamp": ts, "service": svc, "line": line}


CONSUMED = _rec(1000, "telikos-booking-service",
                '{"level":"INFO","message":"New booking event successfully consumed '
                f'servicePlanNo : {CASE}"}}')
FAILED = _rec(2000, "telikos-booking-service",
              '{"level":"INFO","message":"ActivityPlan : {\\"orderId\\":\\"' + CASE + '\\",'
              '\\"status\\":\\"FAILED\\",\\"businessExceptions\\":[{\\"exceptionCategory\\":'
              '\\"Data Validation\\",\\"exceptionCode\\":\\"TEST-101\\",\\"exceptionName\\":'
              '\\"Facility code cannot be empty\\"}]}"}')
VALIDATION = _rec(1990, "telikos-booking-service",
                  '{"level":"INFO","message":"mandatory-field validation found 1 failure(s) for '
                  f'servicePlanNumber {CASE}"}}')
HTTP = _rec(2010, "telikos-booking-service",
            '{"level":"WARN","message":"Registration failed for orderId X status=400 BAD_REQUEST"}')


# --- P2: what failed, at any level -------------------------------------------------

def test_a_step_that_failed_at_info_is_a_failure_signal():
    out = booking_case.failure_signals([CONSUMED, VALIDATION, FAILED, HTTP])
    assert "TEST-101: Facility code cannot be empty" in out
    assert "validation found 1 failure" in out and "status=400" in out
    assert "consumed" not in out


def test_a_clean_trail_has_no_failure_signals():
    assert "none in this window" in booking_case.failure_signals([CONSUMED])


# --- P1: the flow comes from the workspace's documents -------------------------------

def _docs(tmp_path, monkeypatch):
    from app import project_knowledge
    files = {
        "runtime/chain.md": "---\ntitle: The chain\nsummary: every hop\ncapabilities: [cross-repo-tracing]\n---\n"
                            "# Chain\nintake → planning → dispatch → notice",
        "runtime/dispatch-flow.md": "---\ntitle: Dispatch\nsummary: how dispatch runs and fails\n---\n# x",
        "operations/failure-model.md": "---\ntitle: Failures\nsummary: where it fails\n---\n# y",
        "operations/monitoring.md": "---\ntitle: Monitoring\nsummary: dashboards\n---\n# z",
        "contracts/schemas.md": "---\ntitle: Schemas\nsummary: avro\n---\n# s",
    }
    out = []
    for rel, body in files.items():
        p = tmp_path / "svc" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        out.append((p, "svc"))
    monkeypatch.setattr(project_knowledge, "_repo_files", lambda ws: out)


def test_the_flow_is_read_from_the_workspace_not_written_in_code(tmp_path, monkeypatch):
    _docs(tmp_path, monkeypatch)
    out = booking_case.flow_knowledge("ws")
    assert "intake → planning → dispatch → notice" in out, "the chain in full"
    assert "svc/runtime/dispatch-flow.md — how dispatch runs and fails" in out
    assert "failure-model.md" in out
    assert "monitoring.md" not in out and "schemas.md" not in out


def test_the_case_brief_and_the_flow_say_first_step_not_completed_and_never_all_good(tmp_path, monkeypatch):
    rule = booking_case.rule(f"check {CASE} in sit")
    assert "FIRST step of the flow that did not complete" in rule and "'Fine' only if" in rule
    _docs(tmp_path, monkeypatch)
    flow = booking_case.flow_knowledge("ws")
    assert "WALK THE FLOW" in flow and "Never answer 'fine'" in flow


def test_the_evidence_carries_failure_signals_and_the_flow(tmp_path, monkeypatch):
    _docs(tmp_path, monkeypatch)
    monkeypatch.setattr(grafana, "enabled", lambda: True)

    async def records(term, ns, minutes=4320):
        return [CONSUMED, VALIDATION, FAILED] if ns == "sit" else []
    monkeypatch.setattr(grafana, "records_for", records)
    brief = "Is it fine?" + booking_case.rule(f"check {CASE} in sit")
    out = asyncio.run(booking_case.evidence(brief, workspace="ws"))
    assert "TEST-101" in out and "intake → planning" in out


# --- P5: the ids they gave a line earlier ------------------------------------------

WINDOW = ("Recent exchange (untrusted chat data):\n"
          "Priya Nair: Can you please review the change\n"
          f"Priya Nair: Check {CASE}, {OTHER}\n"
          "Arun: merged\n"
          "Priya Nair: Fine na??")


def test_fine_na_after_check_x_y_is_the_same_case():
    out = responder._waiting_brief("Priya Nair", "Fine na??", WINDOW, need="is it fine")
    assert f"This is about a specific case — {CASE}, {OTHER}" in out


def test_ids_in_the_ask_itself_still_make_the_case_as_before():
    out = responder._waiting_brief("Priya Nair", f"check {CASE} in sit please", "")
    assert f"This is about a specific case — {CASE}" in out


def test_no_ids_anywhere_adds_no_case():
    out = responder._waiting_brief("Priya Nair", "can we connect at 3?", "Priya Nair: hi")
    assert "This is about a specific case" not in out


def test_only_their_ids_count_not_his():
    out = responder._waiting_brief("Priya Nair", "Fine na??", f"Arun: I checked {CASE}\nPriya Nair: ok")
    assert "This is about a specific case" not in out


# --- P4: a stale case answer is looked at again, not shown again ----------------------

def _answered(minutes_ago, analysis=f"{CASE}: consumed, no errors"):
    t = store.create_task(f"Priya Nair asked: Fine na??", "analysis", "brief", None)
    store.update_task(t["id"], status="done", result="ANALYSIS: x\nREPLY: y",
                      finished_at=time.time() - minutes_ago * 60)
    return t, {"type": "answer", "task_id": t["id"], "who": "Priya Nair", "to": "Priya Nair",
               "need": "is it fine", "analysis": analysis, "what": "ya fine"}


def test_a_case_answer_older_than_half_an_hour_is_rechecked(monkeypatch):
    ran = []

    async def worker(task_id):
        ran.append(task_id)
    monkeypatch.setattr(tasks, "_worker", worker)
    t, item = _answered(65)

    async def go():
        out = await answers._rechecking(item)
        await asyncio.sleep(0)
        return out
    assert asyncio.run(go()) is True and ran == [t["id"]]
    row = store.get_task(t["id"])
    assert row["status"] == "running" and "RE-CHECK NOW" in row["prompt"]
    assert store.kv_get(f"task_asked:{t['id']}") is None, "Asta's re-check is not his ask"


def test_a_fresh_or_non_case_answer_is_shown_as_before(monkeypatch):
    monkeypatch.setattr(tasks, "_worker", lambda tid: None)
    _, fresh = _answered(10)
    _, chat = _answered(65, analysis="they want to connect at 3")
    assert asyncio.run(answers._rechecking(fresh)) is False
    assert asyncio.run(answers._rechecking(chat)) is False


def test_the_rechecked_answer_is_shown_even_if_the_words_are_the_same(monkeypatch):
    shown = []

    async def show(cid, intent):
        shown.append(intent)
    monkeypatch.setattr(answers, "_show", show)
    monkeypatch.setattr(answers, "phone_conversation", lambda: "c-1")
    monkeypatch.setattr(answers, "_blocked", lambda cid, now=None: False)
    monkeypatch.setattr(answers, "origin_allowed", lambda *a, **k: True)
    t, _ = _answered(65)
    store.kv_set(answers._shown_key(t["id"], "Priya Nair"), str(time.time()))
    store.kv_set(answers._RECHECK + str(t["id"]), "10:28")
    asyncio.run(answers.present(who="Priya Nair", need="is it fine", chat="Priya Nair",
                                group=False, analysis=f"{CASE}: RFP failed", reply="not fine yet",
                                task_id=t["id"]))
    assert shown and "Re-checked now — the first look was at 10:28" in shown[0].get("note", "")
