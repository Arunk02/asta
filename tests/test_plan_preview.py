"""He approves the plan he read — all of it — and the worker builds against all of it."""
import asyncio

import pytest

from pathlib import Path

from app import go, notify, plan_pdf, presence, store, task_spec, tasks

STEPS = "\n".join(f"{i}. In Service{i}.java change step {i} so the retry is capped and logged"
                  for i in range(1, 80))
LONG = f"STRUCTURE\n  BookingService  validates\n\nSTEPS\n{STEPS}\n\nRISK: medium\n\nPLAN READY"
SHORT = "1. Change the validator\n2. Add tests\n\nPLAN READY"


@pytest.fixture
def phone(monkeypatch):
    out = {"messages": [], "documents": [], "at_laptop": False}

    async def _notify(text, *a, **k):
        out["messages"].append(text)
        return {}

    async def document(path, caption=""):
        # The .md beside what was sent is the plan's text; the PDF is its rendering.
        out["documents"].append((path, Path(path).with_suffix(".md").read_text()))
        return True

    async def at_laptop():
        return out["at_laptop"]

    monkeypatch.setattr(notify, "notify", _notify)
    monkeypatch.setattr(notify, "wa_document", document)
    monkeypatch.setattr(presence, "at_laptop", at_laptop)
    monkeypatch.setattr(go, "no_ask", lambda *a, **k: "")
    return out


def _code(title="Cap the retry", workspace=None):
    return store.create_task(title, "code", "x", workspace)["id"]


def test_a_plan_too_long_for_the_phone_arrives_in_full_as_a_file(phone):
    tid = _code()
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), LONG))
    (path, body), = phone["documents"]
    assert path.endswith(f"plan-{tid}.pdf") and body.startswith(f"# Plan for task #{tid} — Cap the retry")
    assert Path(path).read_bytes().startswith(b"%PDF")
    assert "79. In Service79.java" in body and "1. In Service1.java" in body
    assert "whole plan is attached" in phone["messages"][-1]
    assert f"approve task {tid}" in phone["messages"][-1]
    assert len(phone["messages"][-1]) < 1300, "the card is a summary once the file carries it"


def test_every_plan_arrives_as_a_file_even_a_short_one(phone):
    """His words, 15 Nov: "send the plan.md file in whatsapp since message is
    not comfortable to read the whole plan"."""
    tid = _code()
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), SHORT))
    (path, body), = phone["documents"]
    assert path.endswith(f"plan-{tid}.pdf") and "Change the validator" in body


def test_at_the_laptop_the_plan_file_still_goes_with_its_card(phone):
    """The card itself reaches his phone wherever he is; a card that says the
    plan is attached and is not would be worse than either."""
    phone["at_laptop"] = True
    tid = _code()
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), LONG))
    assert len(phone["documents"]) == 1 and "attached" in phone["messages"][-1]


def test_when_the_phone_cannot_take_the_file_the_path_is_given(phone, monkeypatch):
    async def down(path, caption=""):
        return False
    monkeypatch.setattr(notify, "wa_document", down)
    tid = _code()
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), LONG))
    assert f"plan-{tid}.pdf" in phone["messages"][-1]


def test_the_plan_reads_as_a_pdf_not_raw_markdown(phone):
    """18 Nov: "the .md file … I don't feel it easy to read". What he opens is a
    PDF with the machine noise taken out."""
    tid = _code()
    plan = ("STRUCTURE\n  A.java  validates\n\n**Steps**\n1. Change `A.check`\n\n"
            "RISK: low\n\nPLAN READY\n\n```outcome\n{\"kind\": \"plan_ready\"}\n```\n")
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), plan))
    (path, _), = phone["documents"]
    cleaned = plan_pdf.clean(plan)
    assert "### Structure" in cleaned and "**Risk:** low" in cleaned
    assert "outcome" not in cleaned and "PLAN READY" not in cleaned
    assert Path(path).suffix == ".pdf"


def test_when_no_pdf_can_be_made_the_md_is_not_sent_and_the_card_carries_the_plan(phone, monkeypatch):
    """25 Nov, his words: remove sending the plan.md. Without any PDF the plan
    still reaches him — as text on the card, not as a raw .md file."""
    monkeypatch.setattr(plan_pdf, "render", lambda *a, **k: None)
    tid = _code()
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), LONG))
    assert not any(p.endswith(".md") for p, _ in phone["documents"])
    assert any("In Service" in m for m in phone["messages"])


def test_a_big_change_is_asked_for_a_drawn_diagram():
    assert "add a `DIAGRAM` block" in tasks.CODE_OVERRIDES and "```mermaid" in tasks.CODE_OVERRIDES


def test_a_revised_plan_is_a_new_file_and_the_one_he_read_is_kept():
    tid = _code()
    first = task_spec.preview(tid, LONG, "Cap the retry")
    second = task_spec.preview(tid, LONG + "\n80. one more step", "Cap the retry")
    assert first != second and first.exists() and "80. one more step" not in first.read_text()


def test_the_worker_file_ends_with_a_receipt_and_the_pointer_asks_for_it(monkeypatch):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    tid = _code()
    pointer = task_spec.prompt_block(tid, LONG)
    path = task_spec.artifact(tid, LONG)
    assert str(path) in pointer and "quote that line" in pointer
    assert path.read_text().rstrip().endswith(task_spec.receipt(LONG))


def test_unread_is_only_checked_when_the_worker_was_given_a_pointer(monkeypatch):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    tid = _code()
    store.kv_set(f"task_spec:{tid}", SHORT)
    assert task_spec.unread(tid, "done") == ""
    store.kv_set(f"task_spec:{tid}", LONG)
    assert task_spec.unread(tid, "done") == task_spec.receipt(LONG)
    assert task_spec.unread(tid, f"done\n{task_spec.receipt(LONG)}") == ""


@pytest.mark.parametrize("quoted, flagged", [(True, False), (False, True)])
def test_an_implementation_that_did_not_quote_the_receipt_is_flagged(monkeypatch, quoted, flagged):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    tid = _code()
    store.kv_set(f"task_spec:{tid}", LONG)
    finished = {}

    async def leg(task_id, prompt, cwd, **kw):
        assert task_spec.receipt(LONG) not in prompt, "the receipt is only in the file"
        return "Implemented." + (f"\n{task_spec.receipt(LONG)}" if quoted else "")

    async def finish(task_id, t, result, hops=0):
        finished["result"] = result

    monkeypatch.setattr(tasks, "_run_code_leg", leg)
    monkeypatch.setattr(tasks, "_finish_code", finish)
    asyncio.run(tasks._resume_worker(tid, "PLAN APPROVED", approved=True))
    assert ("Not confirmed against the full approved plan" in finished["result"]) is flagged


COMPLETE = """STRUCTURE
  EtaValidator   rejects late ETA
FLOW
  vessel feed    sends the ETA
1. Add the validator → test asserts rejection
RISK: low
ASSUMPTIONS: none
CONTEXT USED: flow docs
ROLLBACK: revert the commit
SCOPE: PR only
PLAN READY"""


def test_a_complete_plan_names_nothing_missing():
    assert tasks.missing_parts(COMPLETE) == []


def test_the_card_names_what_a_plan_left_out(phone):
    tid = _code()
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), SHORT))
    assert "does not state: STRUCTURE, FLOW, RISK, ASSUMPTIONS, CONTEXT USED, ROLLBACK, SCOPE" \
        in phone["messages"][-1]
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), COMPLETE))
    assert "does not state" not in phone["messages"][-1]
