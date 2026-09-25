"""His own documents, searchable with citations — Round 3, P13.

"A folder per workspace, indexed, with cited search."

The two that prompted it: a 4 MB Word file describing the NAM inland booking
flow, and a PDF of the Telikos end-to-end flow. Neither is in a repo, neither is
on Confluence in a form a brain can read, and both answer questions Asta was
otherwise guessing at — which is the worst of the three options, because a guess
about a booking flow reads exactly like knowledge.

Three things decide the shape of this module:

  A CITATION OR IT DID NOT HAPPEN. Every passage comes back with the document it
  came from and the page or heading it sits on. An answer nobody can check is a
  more confident way to be wrong, and these documents exist precisely because
  the cost of being confidently wrong about the booking flow is high.

  NOTHING LEAVES THE LAPTOP. SQLite's own full-text index, no embedding service,
  no upload. That is a privacy decision first — these are internal documents —
  and it also means search works with no network, costs nothing per query, and
  cannot fail because a model was busy. BM25 over his own prose is not worse
  than embeddings at finding the paragraph that mentions the thing he asked
  about; it is just less fashionable.

  STALE IS WORSE THAN MISSING. A document whose bytes changed is read again; one
  that was deleted stops answering. An index that quietly serves last week's
  rule is the failure this must not have — "missing" sends him to look, "stale"
  does not.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from pathlib import Path

#: Where his documents live. One folder, in his home, with a name he can see.
DEFAULT_DIR = "~/Asta knowledge"

#: What can be read. Anything else in the folder is left alone rather than
#: turned into nonsense text — a PNG indexed as mojibake is a search result that
#: wastes his time twice.
READABLE = (".md", ".markdown", ".txt", ".pdf", ".docx", ".rst", ".csv")

#: Passages are the unit of both search and citation. Big enough to be an answer
#: on their own, small enough that the citation points somewhere specific.
PASSAGE_CHARS = int(os.environ.get("ASTA_KNOWLEDGE_PASSAGE", "1200"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_docs (
  path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, folder TEXT NOT NULL,
  read_at REAL NOT NULL, passages INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_passages USING fts5(
  document, where_, body, path UNINDEXED, tokenize='porter unicode61'
);
"""


def folder(workspace: str = "") -> Path:
    """The knowledge folder — his by default, or one belonging to a workspace.

    A workspace folder is how a repo carries its own documents without them
    being mixed into everything else; nothing sets one today, and the point is
    that it does not need a second module when something does.
    """
    named = os.environ.get("ASTA_KNOWLEDGE_DIR", "").strip()
    root = Path(named).expanduser() if named else Path(DEFAULT_DIR).expanduser()
    if workspace:
        root = root / workspace
    with contextlib.suppress(Exception):
        root.mkdir(parents=True, exist_ok=True)
    return root


# --- reading a document ------------------------------------------------------

def read(path: Path) -> list[tuple[str, str]]:
    """[(where, text)] — the document as anchored chunks, or [] if unreadable.

    `where` is what a citation points at: a page for a PDF, the heading above
    the text for everything else. It is never empty, because "somewhere in a
    120-page document" is not a citation.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _read_pdf(path)
        if suffix == ".docx":
            return _read_docx(path)
        return _by_heading(path.read_text(errors="replace"))
    except Exception:                                           # noqa: BLE001
        # One unreadable file must never stop the other nineteen being indexed.
        return []


def _read_pdf(path: Path) -> list[tuple[str, str]]:
    from pypdf import PdfReader
    out = []
    for n, page in enumerate(PdfReader(str(path)).pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            out.append((f"page {n}", text))
    return out


def _read_docx(path: Path) -> list[tuple[str, str]]:
    """Word, kept under its own headings — and its TABLES, which is where the
    interesting half of a flow document usually lives."""
    import docx
    doc = docx.Document(str(path))
    out: list[tuple[str, str]] = []
    heading, buf = "", []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            out.append((heading or "start of document", body))
        buf.clear()

    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        if (para.style.name or "").lower().startswith("heading"):
            flush()
            heading = text
        else:
            buf.append(text)
    flush()
    for n, table in enumerate(doc.tables, start=1):
        rows = [" | ".join((c.text or "").strip() for c in r.cells) for r in table.rows]
        body = "\n".join(r for r in rows if r.strip(" |"))
        if body:
            out.append((f"table {n}" + (f" under {heading}" if heading else ""), body))
    return out


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)


def _by_heading(text: str) -> list[tuple[str, str]]:
    """Markdown and plain text, split at its headings."""
    marks = list(_HEADING.finditer(text or ""))
    if not marks:
        body = (text or "").strip()
        return [("start of document", body)] if body else []
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if body:
            out.append((m.group(2), body))
    head = text[:marks[0].start()].strip()
    if head:
        out.insert(0, ("start of document", head))
    return out


def _passages(chunks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Split anything too long to cite precisely, keeping its anchor."""
    out = []
    for where, body in chunks:
        if len(body) <= PASSAGE_CHARS:
            out.append((where, body))
            continue
        parts = re.split(r"\n\s*\n", body)
        buf = ""
        for part in parts:
            if len(buf) + len(part) > PASSAGE_CHARS and buf:
                out.append((where, buf.strip()))
                buf = ""
            buf += part + "\n\n"
        if buf.strip():
            out.append((where, buf.strip()))
    return out


# --- the index ---------------------------------------------------------------

def _fingerprint(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]


def reindex(workspace: str = "") -> dict:
    """Bring the index in line with the folder. {documents, read, passages}.

    Only what changed is read: a 4 MB Word file takes a moment, and doing that
    on every search would make the feature something he stops using.
    """
    from . import store
    root = folder(workspace)
    with store._connect() as conn:
        conn.executescript(_SCHEMA)
    found = {p for p in root.rglob("*")
             if p.is_file() and p.suffix.lower() in READABLE and not p.name.startswith(".")}
    read_count = passages = 0
    with store._connect() as conn:
        known = {r["path"]: r["fingerprint"] for r in
                 conn.execute("SELECT path, fingerprint FROM knowledge_docs WHERE folder=?",
                              (str(root),)).fetchall()}
        for path in sorted(found):
            key, fp = str(path), _fingerprint(path)
            if known.get(key) == fp:
                continue
            chunks = _passages(read(path))
            if not chunks:
                # Unreadable or empty: forget it rather than leave yesterday's
                # text answering for a file that no longer says it.
                conn.execute("DELETE FROM knowledge_passages WHERE path=?", (key,))
                conn.execute("DELETE FROM knowledge_docs WHERE path=?", (key,))
                found.discard(path)
                continue
            conn.execute("DELETE FROM knowledge_passages WHERE path=?", (key,))
            conn.executemany(
                "INSERT INTO knowledge_passages (document, where_, body, path) VALUES (?,?,?,?)",
                [(path.name, where, body, key) for where, body in chunks])
            conn.execute(
                "INSERT INTO knowledge_docs (path, fingerprint, folder, read_at, passages) "
                "VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, read_at=excluded.read_at, "
                "passages=excluded.passages",
                (key, fp, str(root), __import__("time").time(), len(chunks)))
            read_count += 1
            passages += len(chunks)
        # Deleted on disk means deleted here. An index that still answers from a
        # document he threw away is worse than one that says it knows nothing.
        alive = {str(p) for p in found}
        for gone in set(known) - alive:
            conn.execute("DELETE FROM knowledge_passages WHERE path=?", (gone,))
            conn.execute("DELETE FROM knowledge_docs WHERE path=?", (gone,))
    return {"documents": len(found), "read": read_count, "passages": passages}


# --- searching ---------------------------------------------------------------

#: Words that carry no meaning in a search over his own documents. People type
#: questions, not keywords, and "what happens when" matches every page.
_NOISE = frozenset("""a an the is are was were be been being do does did doing what
when where which who whom why how if then than that this these those and or but for
nor so yet of in on at to from by with about into over after before under it its
can could should would will shall may might must have has had i we you they me us
them my our your their there here not no yes please tell show explain happens
happen need want know""".split())


def _query(text: str) -> str:
    """An FTS query from the sentence he typed."""
    words = [w for w in re.findall(r"[A-Za-z0-9_]+", (text or "").lower())
             if len(w) > 1 and w not in _NOISE]
    return " OR ".join(f'"{w}"' for w in words[:24])


def search(question: str, limit: int = 5, workspace: str = "") -> list[dict]:
    """The passages that answer this, best first. [] when nothing matches."""
    from . import store
    query = _query(question)
    if not query:
        return []
    root = str(folder(workspace))
    with contextlib.suppress(Exception):
        with store._connect() as conn:
            conn.executescript(_SCHEMA)
            rows = conn.execute(
                "SELECT p.document, p.where_, p.body, p.path, bm25(knowledge_passages) AS score "
                "FROM knowledge_passages p JOIN knowledge_docs d ON d.path = p.path "
                "WHERE knowledge_passages MATCH ? AND d.folder = ? "
                "ORDER BY score LIMIT ?", (query, root, limit)).fetchall()
        return [{"document": r["document"], "where": r["where_"], "text": r["body"],
                 "path": r["path"], "score": round(-float(r["score"]), 2)} for r in rows]
    return []


def cite(hits: list[dict]) -> str:
    """The citations for a set of passages, as he would write them."""
    seen, out = set(), []
    for h in hits:
        mark = f"{h['document']} ({h['where']})"
        if mark not in seen:
            seen.add(mark)
            out.append(mark)
    return "; ".join(out)


def answer(question: str, limit: int = 3, workspace: str = "") -> str:
    """What his documents say about this, with the citations attached.

    Passages, not a summary: summarising here would put a model between him and
    his own document for no gain, and every word it invented would arrive
    wearing a citation.
    """
    hits = search(question, limit, workspace)
    if not hits:
        return ("Nothing in his knowledge folder covers that — "
                f"searched {folder(workspace)}.")
    parts = [f"**{h['document']} — {h['where']}**\n{h['text'].strip()}" for h in hits]
    return "\n\n".join(parts) + f"\n\nFrom: {cite(hits)}"
