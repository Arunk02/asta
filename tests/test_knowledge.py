"""His own documents, searchable with citations — Round 3, P13.

"A folder per workspace, indexed, with cited search."

The two documents that prompted it are a 4 MB Word file describing the NAM
inland booking flow and a PDF of the Telikos end-to-end flow. Neither is in any
repo, neither is on Confluence in a form a brain can read, and both answer
questions Asta was otherwise guessing at.

What makes this different from pasting them into a prompt:

  A CITATION OR IT DID NOT HAPPEN. Every passage comes back with the document it
  is from and the page or heading it is on, so an answer can be checked. A
  knowledge base that cannot be checked is a more confident way to be wrong.

  NOTHING LEAVES THE LAPTOP. Full-text search in SQLite, no embedding service,
  no upload. It also means it works with no network and costs nothing per query.

  STALE IS WORSE THAN MISSING. A document that changed on disk is re-read; one
  that was deleted stops being searchable. An index that quietly serves last
  week's answer is the failure this must not have.
"""

from __future__ import annotations

import pytest

from app import knowledge, store


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    monkeypatch.setenv("ASTA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    store.init()
    (tmp_path / "knowledge").mkdir()
    yield


def _write(name: str, body: str) -> None:
    import os
    from pathlib import Path
    p = Path(os.environ["ASTA_KNOWLEDGE_DIR"]) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


# --- what gets read ---------------------------------------------------------------------

def test_the_folder_is_his_and_is_made_if_it_is_not_there():
    assert knowledge.folder().name == "knowledge"
    assert knowledge.folder().is_dir()


def test_plain_documents_are_read_and_found():
    _write("booking.md", "# Inland booking\n\nA transport order is created when "
                         "the callback arrives from CAMS.\n")
    assert knowledge.reindex()["documents"] == 1
    hits = knowledge.search("transport order")
    assert hits and "transport order" in hits[0]["text"].lower()


def test_every_hit_says_where_it_came_from():
    """An answer nobody can check is a more confident way to be wrong."""
    _write("booking.md", "# Inland booking\n\nThe STF is raised after the "
                         "transport order is confirmed.\n")
    knowledge.reindex()
    hit = knowledge.search("STF")[0]
    assert hit["document"] == "booking.md"
    assert hit["where"], "no page or heading to cite"
    assert "booking.md" in knowledge.cite([hit])


def test_a_heading_is_the_anchor_for_a_word_document():
    _write("flow.md", "# Overview\n\nNothing here.\n\n## Cancellation\n\n"
                      "A cancelled booking must not update the transport order.\n")
    knowledge.reindex()
    hit = knowledge.search("cancelled booking")[0]
    assert "Cancellation" in hit["where"]


# --- staying honest ----------------------------------------------------------------------

def test_a_document_that_changed_is_read_again():
    """An index that quietly serves last week's answer is the failure here."""
    _write("booking.md", "The old rule was to retry quibblesnort times.\n")
    knowledge.reindex()
    assert knowledge.search("quibblesnort")
    _write("booking.md", "The rule is now to retry flumberwock times.\n")
    assert knowledge.reindex()["documents"] == 1
    # The old wording is gone, not merely outranked: a search matches on ANY
    # word, so "still returns something" would not have proved anything.
    assert knowledge.search("quibblesnort") == []
    assert knowledge.search("flumberwock")


def test_a_document_that_was_deleted_stops_answering():
    import os
    from pathlib import Path
    _write("gone.md", "This mentions a unique word: quibblesnort.\n")
    knowledge.reindex()
    assert knowledge.search("quibblesnort")
    (Path(os.environ["ASTA_KNOWLEDGE_DIR"]) / "gone.md").unlink()
    knowledge.reindex()
    assert knowledge.search("quibblesnort") == []


def test_reindexing_an_unchanged_folder_does_no_work():
    _write("booking.md", "A transport order is created on callback.\n")
    knowledge.reindex()
    assert knowledge.reindex()["read"] == 0, "re-read a document that had not changed"


def test_an_empty_folder_is_not_an_error():
    assert knowledge.reindex() == {"documents": 0, "read": 0, "passages": 0}
    assert knowledge.search("anything") == []


def test_a_file_it_cannot_read_is_skipped_not_fatal():
    import os
    from pathlib import Path
    (Path(os.environ["ASTA_KNOWLEDGE_DIR"]) / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _write("booking.md", "A transport order is created on callback.\n")
    assert knowledge.reindex()["documents"] == 1


# --- searching ---------------------------------------------------------------------------

def test_the_best_passage_comes_first():
    _write("a.md", "Booking is mentioned once here.\n")
    _write("b.md", "Booking booking booking — this section is all about booking "
                   "and the booking lifecycle.\n")
    knowledge.reindex()
    assert knowledge.search("booking")[0]["document"] == "b.md"


def test_a_question_finds_the_passage_not_the_question_words():
    """People search with a sentence, not with keywords."""
    _write("flow.md", "## Cancellation\n\nWhen a booking is cancelled the "
                      "transport order must be released before the STF is raised.\n")
    knowledge.reindex()
    hits = knowledge.search("what happens to the transport order when a booking is cancelled?")
    assert hits and "released" in hits[0]["text"]


def test_nothing_found_is_said_plainly_rather_than_answered_anyway():
    _write("flow.md", "Only about inland bookings.\n")
    knowledge.reindex()
    assert knowledge.search("kubernetes ingress") == []
    assert "nothing" in knowledge.answer("kubernetes ingress").lower()


def test_an_answer_carries_its_citations():
    _write("flow.md", "## Cancellation\n\nA cancelled booking releases the "
                      "transport order.\n")
    knowledge.reindex()
    said = knowledge.answer("what happens when a booking is cancelled")
    assert "flow.md" in said and "Cancellation" in said


# --- the two formats his own documents are actually in -----------------------------------

def _docx(name: str, headings: list[tuple[str, str]], table: list[list[str]] | None = None):
    import os
    from pathlib import Path

    import docx
    doc = docx.Document()
    for heading, body in headings:
        doc.add_heading(heading, level=1)
        doc.add_paragraph(body)
    if table:
        t = doc.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, cell in enumerate(row):
                t.cell(r, c).text = cell
    doc.save(str(Path(os.environ["ASTA_KNOWLEDGE_DIR"]) / name))


def test_a_word_document_is_read_under_its_own_headings():
    """His inland booking documentation is a 4 MB .docx, and the headings are
    what a citation can point at."""
    _docx("booking.docx", [
        ("Overview", "This describes the NAM inland booking flow."),
        ("Cancellation", "A cancelled booking releases the transport order "
                         "before the STF is raised."),
    ])
    assert knowledge.reindex()["documents"] == 1
    hit = knowledge.search("cancelled booking transport order")[0]
    assert hit["document"] == "booking.docx"
    assert "Cancellation" in hit["where"]
    assert "releases the transport order" in hit["text"]


def test_the_tables_in_a_word_document_are_read_too():
    """Half of a flow document is tables — status codes, field mappings — and a
    reader that skips them silently answers "not covered" about the part that
    was covered best."""
    _docx("fields.docx", [("Field mapping", "The mapping is below.")],
          table=[["Field", "Source"], ["quibblesnort", "CAMS callback"]])
    knowledge.reindex()
    hits = knowledge.search("quibblesnort")
    assert hits and "CAMS callback" in hits[0]["text"]
    assert "table" in hits[0]["where"]


def _pdf(name: str, pages: list[str]) -> None:
    """A minimal, real PDF — enough for pypdf to extract the text back."""
    import os
    from pathlib import Path
    objects, kids = [], []
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        content_id, page_id = 4 + i * 2, 5 + i * 2
        objects.append((content_id, b"<< /Length %d >>\nstream\n%s\nendstream"
                        % (len(stream), stream)))
        objects.append((page_id,
                        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                        b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
                        % content_id))
        kids.append(page_id)
    head = [(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
            (2, b"<< /Type /Pages /Kids [%s] /Count %d >>"
                % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))),
            (3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")]
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for num, body in head + objects:
        offsets[num] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (num, body)
    start = len(out)
    top = max(offsets) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % top
    for num in range(1, top):
        out += b"%010d 00000 n \n" % offsets.get(num, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (top, start)
    (Path(os.environ["ASTA_KNOWLEDGE_DIR"]) / name).write_bytes(bytes(out))


def test_a_pdf_is_read_and_cited_by_page():
    """The Telikos end-to-end flow is a PDF, and "somewhere in it" is not a
    citation — the page number is the whole point."""
    _pdf("flow.pdf", ["Page one is about bookings.",
                      "The quibblesnort service publishes to the queue."])
    assert knowledge.reindex()["documents"] == 1
    hit = knowledge.search("quibblesnort")[0]
    assert hit["document"] == "flow.pdf" and hit["where"] == "page 2"
    assert "flow.pdf (page 2)" in knowledge.cite([hit])


def test_a_paragraph_with_no_style_does_not_lose_the_document():
    """His real 4 MB .docx has paragraphs whose style is None — python-docx
    allows it and a generated test file never produces it. `para.style.name`
    raised AttributeError, the blanket except returned [], and a 105-paragraph
    document was silently skipped with no error anywhere."""
    import os
    from pathlib import Path

    import docx
    doc = docx.Document()
    doc.add_heading("Cancellation", level=1)
    para = doc.add_paragraph("A cancelled booking releases the transport order.")
    para.style = None                                    # what his document has
    doc.save(str(Path(os.environ["ASTA_KNOWLEDGE_DIR"]) / "styleless.docx"))
    assert knowledge.reindex()["documents"] == 1
    assert knowledge.search("cancelled booking transport order")


def test_a_document_that_cannot_be_read_is_reported_not_swallowed():
    """Silently skipping is how a 4 MB document was missing from the index with
    nothing anywhere to say so. Unreadable must be loud."""
    import os
    from pathlib import Path
    # A .docx that is not a zip at all: python-docx raises, and it must say so.
    (Path(os.environ["ASTA_KNOWLEDGE_DIR"]) / "broken.docx").write_bytes(b"not a docx")
    out = knowledge.reindex()
    assert out["documents"] == 0
    assert out["unreadable"] and "broken.docx" in out["unreadable"][0]
