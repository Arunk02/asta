"""One lookup for the project: his documents (the existing index) and the repo
summaries, misheard words corrected, a few short passages out.

2 Oct: "what is Telikos Inland Booking?" was answered "I have nothing on that"
with his documents indexed since 25 Sep and every repo written up on disk."""

from __future__ import annotations

import asyncio
import time

import pytest

from app import knowledge, project_knowledge as pk, store, voice_mode as vm


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTA_KNOWLEDGE_DIR", str(tmp_path / "docs"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "nam-inland-booking-guide.md").write_text(
        "# NAM Inland Booking — User Documentation\n\n"
        "## Source Systems at a Glance\n"
        "CMD provides customer code details. CCD provides contacts and free days. "
        "mEPC provides the route. Athena Lite provides pricing on the offer.\n\n"
        "## Telikos Inland Booking journey\n"
        "Telikos inland booking covers search offers, select departure, add details, review, confirm.\n")
    (docs / "runbooks.md").write_text(
        "# Runbooks\n\n## Ready for Invoicing stuck at Pending\n"
        "Usually missing finance data such as the VAT partner code; add it in FinOps.\n")
    repo = tmp_path / "repos" / "telikos-booking-service" / "runtime"
    repo.mkdir(parents=True)
    (repo / "send-to-tms.md").write_text(
        "---\ntitle: send to TMS\nsummary: Covers the SEND_TO_TMS workflow leg and its guards\n"
        "sources: [x.java]\n---\n# Send to TMS\nThe workflow validates the booking, then publishes.\n")
    monkeypatch.setattr(pk, "_repo_files", lambda ws: [
        (p, "telikos-booking-service") for p in sorted((tmp_path / "repos").rglob("*.md"))])
    monkeypatch.setattr(pk, "default_workspace", lambda: "booking")
    pk._STATE["reindexed"] = 0.0
    pk._REPOS.clear()
    pk._VOCAB.update(words=None, at=0.0)
    knowledge.reindex()
    return tmp_path


def test_his_question_finds_his_document(world):
    hits = pk.search("which system provides pricing")
    assert hits and hits[0]["source"] == "doc" and "Athena Lite" in hits[0]["text"]


def test_a_misheard_word_is_corrected_to_the_one_the_knowledge_uses(world):
    assert "telikos" in pk.corrected("what is Telecos inland booking")
    hits = pk.search("Can you explain to me what is Telecos Inland Booking it is?")
    assert hits and hits[0]["source"] == "doc" and "inland" in hits[0]["text"].lower()


def test_the_repo_summaries_are_searched_without_their_file_header(world):
    hits = [h for h in pk.search("send to TMS workflow guards") if h["source"] != "doc"]
    assert hits and hits[0]["source"] == "telikos-booking-service"
    assert "sources:" not in hits[0]["text"] and "SEND_TO_TMS" in hits[0]["text"]


def test_the_workspace_overview_names_every_repo(world):
    hits = pk.search("what are the repos in the workspace")
    over = [h for h in hits if h["source"] == "workspace"]
    assert over and "telikos-booking-service" in over[0]["text"] and "SEND_TO_TMS" in over[0]["text"]


def test_what_goes_out_is_small(world):
    (world / "docs" / "big.md").write_text("# Big\n\n" + ("pricing details line. " * 3000))
    pk._STATE["reindexed"] = 0.0
    out = pk.lookup("pricing", budget=1500)
    assert 0 < len(out) <= 1500


def test_it_is_fast(world):
    pk.search("warm")
    started = time.time()
    for _ in range(20):
        pk.search("which system provides pricing")
    assert (time.time() - started) / 20 < 0.05


def test_what_is_a_question_for_the_knowledge_and_check_is_work():
    assert vm.knows_about("Can you explain to me what is Telecos Inland Booking it is?")
    assert vm.knows_about("Which system provides the pricing on the offer?")
    assert vm.knows_about("Okay, can you tell about a tele booking service?")
    assert not vm.knows_about("check booking H69LMCN6KZY in UAT")
    assert not vm.knows_about("what is the H6FP98C8LK7 issue")


def test_a_voice_question_is_answered_from_the_knowledge_without_a_job(world, monkeypatch):
    from app import frontdesk, main, voice, voice_talker
    monkeypatch.setattr(frontdesk, "answer_from_state", lambda text: None)

    async def route(text, context=None):
        return voice_talker.DO

    got: list[dict] = []

    async def sentences(text, kind="said", timeout=30, context=""):
        got.append({"kind": kind, "context": context})
        yield "Athena Lite provides the pricing."

    async def dispatch(*a, **k):
        raise AssertionError("no job needed")

    async def speak(text, **k):
        return b"RIFF"

    class H:
        async def send_text(self, t):
            pass

    monkeypatch.setattr(voice_talker, "route", route)
    monkeypatch.setattr(voice_talker, "sentences", sentences)
    monkeypatch.setattr(main, "_dispatch", dispatch)
    monkeypatch.setattr(voice, "speak", speak)
    vm._STATE.update(speaker=True, mic=True, mic_on_at=time.time(), helper=H())
    try:
        out = asyncio.run(vm.handle("Which system provides the pricing on the offer?"))
    finally:
        vm._STATE.update(speaker=False, mic=False, helper=None)
    assert out["did"] == "answered" and got and got[0]["kind"] == "to_you"
    assert "Athena Lite" in got[0]["context"]


def test_the_workspace_lookup_carries_the_passages(world, monkeypatch):
    from app.workspace.providers import indexed
    monkeypatch.setattr(pk, "workspace_for_root", lambda root: "booking")
    p = indexed.IndexedProvider.__new__(indexed.IndexedProvider)
    p.root = world
    (world / ".asta-context").mkdir()
    (world / ".asta-context" / indexed.RESOLVER).write_text("console.log('{}')")

    async def fresh(self):
        return "[fresh]"

    monkeypatch.setattr(indexed.IndexedProvider, "freshness", fresh)
    monkeypatch.setattr(indexed, "context_dirname", lambda root: ".asta-context")
    out = asyncio.run(p.resolve("which system provides pricing"))
    assert "Project knowledge" in out and "Athena Lite" in out


def test_the_existing_document_search_is_untouched():
    assert knowledge.DEFAULT_DIR == "~/Asta knowledge" and callable(knowledge.relevant)
    assert store is not None


# --- every channel, one lookup -------------------------------------------------------

def test_a_whatsapp_question_carries_docs_and_code_summaries(world):
    from app import copilot_cli
    ctx = copilot_cli.turn_context("how does the send to TMS workflow guard work?")
    assert "From his project knowledge" in ctx and "SEND_TO_TMS" in ctx
    plain = copilot_cli.turn_context("send Vinish the PR link")
    assert "project knowledge" not in plain, "only questions carry passages"


def test_a_misheard_question_on_whatsapp_still_finds_the_document(world):
    from app import copilot_cli
    ctx = copilot_cli.turn_context("what is telecos inland booking?")
    assert "From his project knowledge" in ctx and "inland" in ctx.lower()


def test_every_lookup_is_logged_with_what_it_found(world):
    pk.lookup("which system provides pricing", channel="chat")
    with store._connect() as c:
        row = c.execute("SELECT subject, detail FROM outcomes WHERE kind='knowledge' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["subject"] == "chat" and "doc" in row["detail"] and "pricing" in row["detail"]


def test_his_correction_goes_into_the_flow_document_and_ranks_first(world):
    assert pk.learn("Email only goes on booking confirmation and send to execution, not every milestone",
                    "Emails go only on booking confirmation and send to execution.")
    doc = (knowledge.folder() / pk.FLOW_DOC).read_text()
    assert "## Facts from Arun" in doc and "only on booking confirmation" in doc
    assert not (knowledge.folder() / "arun-corrections.md").exists(), "no side list"
    hits = pk.search("when does email service send email milestone")
    assert hits and pk.FLOW_DOC.split(".")[0] in hits[0]["where"]
