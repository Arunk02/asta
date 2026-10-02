"""What Asta knows about the project, in one lookup: his documents AND the
written summaries of every repo in the workspace — a few short passages, not
whole files.

Why (2 Oct): asked by voice "what is Telikos Inland Booking?", Asta said it had
nothing, asked where he saw the term, and went reading the BEP Telikos chat —
four times. His documents were indexed since 25 Sep (`knowledge`, ~/Asta
knowledge), and every flow, contract and event of the three booking repos is
written up under the workspace's context folder. Three things kept them apart:

  * voice and voice jobs never asked either;
  * "Telecos Inland Booking" — the recognizer's spelling — matched nothing, and
    `knowledge.relevant` wants 60% of the words to appear;
  * the repo summaries were only ever pointers for a code task to open.

So: misheard words are corrected against the words the knowledge actually uses,
his documents come from the existing index (`knowledge.search`, unchanged),
the repo summaries are ranked here, and what goes out is capped — about 500
tokens per question.
"""

from __future__ import annotations

import difflib
import math
import re
import time
from pathlib import Path

#: What one lookup hands over, at most.
BUDGET_CHARS = 2000
#: A repo-summary section is cut at about this many characters.
CHUNK_CHARS = 1100
#: His documents are re-read for changes at most this often (only changed files are read).
REINDEX_SECONDS = 300.0

_STOP = set("""a an the and or but if then else of to in on at by for with from as is are was were be been
being it its this that these those there here what which who whom whose how why when where do does did done
can could should would will shall may might must i you he she we they me him her us them my your our their
please tell explain about give know like just also any some all more most into out up down over than too very
no not yes ok okay hey hello asta want need get got make made one two three it's""".split())

_STATE: dict = {"reindexed": 0.0}


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 1]


def front_matter(body: str) -> tuple[dict, str]:
    """The `---` block a curated summary starts with: title and summary kept
    (they say what the file is), the rest — sources, hashes — dropped."""
    if not body.startswith("---"):
        return {}, body
    end = body.find("\n---", 3)
    if end < 0:
        return {}, body
    meta = {}
    for line in body[3:end].splitlines():
        m = re.match(r"^(title|summary):\s*(.+)$", line.strip())
        if m:
            meta[m.group(1)] = m.group(2).strip().strip('"')
    return meta, body[end + 4:]


# --- the repo summaries -------------------------------------------------------------

def _repo_files(workspace: str) -> list[tuple[Path, str]]:
    """(file, repo) for every curated summary in the workspace's context index."""
    found: list[tuple[Path, str]] = []
    try:
        from .workspace import registry
        from .workspace.providers.indexed import context_dirname
        ws = registry.get(workspace)
        if ws is not None:
            root = Path(ws.root)
            repos = root / context_dirname(root) / "repos"
            if repos.is_dir():
                for repo in sorted(p for p in repos.iterdir() if p.is_dir()):
                    found += [(p, repo.name) for p in sorted(repo.rglob("*.md"))]
    except Exception:                                          # noqa: BLE001
        pass
    return found


def _sections(path: Path, repo: str) -> list[tuple[str, str, str]]:
    """(repo, label, text) sections of one summary file."""
    try:
        meta, body = front_matter(path.read_text(errors="replace"))
    except OSError:
        return []
    rel = f"{path.parent.name}/{path.name}"
    out: list[tuple[str, str, str]] = []
    heading = meta.get("title", "")
    buf = [meta["summary"]] if meta.get("summary") else []

    def flush():
        text = "\n".join(buf).strip()
        while len(text) > CHUNK_CHARS:
            cut = text.rfind("\n", 0, CHUNK_CHARS)
            cut = cut if cut > CHUNK_CHARS // 3 else CHUNK_CHARS
            out.append((repo, f"{rel} — {heading}".strip(" —"), text[:cut].strip()))
            text = text[cut:].strip()
        if len(text) > 40:
            out.append((repo, f"{rel} — {heading}".strip(" —"), text))

    for line in body.splitlines():
        if re.match(r"^#{1,4}\s", line):
            flush()
            heading, buf = line.lstrip("#").strip()[:90], []
        else:
            buf.append(line)
    flush()
    return out


def _overview(workspace: str, files: list[tuple[Path, str]]) -> str:
    """Every repo and what it does, from the summaries of its own flow and domain
    files — "what are the three repos?" had nothing that answered it whole."""
    lines = []
    for repo in sorted({r for _, r in files}):
        said = []
        for p, r in files:
            if r == repo and p.parent.name in ("runtime", "domain"):
                summary = front_matter(p.read_text(errors="replace"))[0].get("summary")
                if summary:
                    said.append(summary.rstrip("."))
            if len(said) >= 3:
                break
        lines.append(f"- {repo}: " + ("; ".join(said)[:450] if said else "(no summary yet)"))
    return (f"The {workspace} workspace has {len(lines)} repos (services):\n" + "\n".join(lines)) if lines else ""


class _Repos:
    """BM25 over the repo summaries. Built in ~0.1 s, searched in milliseconds."""

    def __init__(self, sections: list[tuple[str, str, str]]):
        self.sections = sections
        docs = [_tokens(f"{label} {label} {text}") for _, label, text in sections]
        self.lens = [len(d) or 1 for d in docs]
        self.avg = sum(self.lens) / max(1, len(self.lens))
        df: dict[str, int] = {}
        self.tf: list[dict[str, int]] = []
        for d in docs:
            counts: dict[str, int] = {}
            for w in d:
                counts[w] = counts.get(w, 0) + 1
            self.tf.append(counts)
            for w in counts:
                df[w] = df.get(w, 0) + 1
        n = len(docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def search(self, words: list[str], k: int) -> list[dict]:
        k1, b = 1.4, 0.75
        scored = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for w in words:
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (k1 + 1) / (f + k1 * (1 - b + b * self.lens[i] / self.avg))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [{"source": self.sections[i][0], "where": self.sections[i][1],
                 "text": self.sections[i][2], "score": round(s, 2)} for s, i in scored[:k]]


_REPOS: dict[str, tuple[tuple, _Repos]] = {}


def _repos(workspace: str) -> _Repos | None:
    files = _repo_files(workspace)
    if not files:
        return None
    sig = tuple((str(p), p.stat().st_mtime) for p, _ in files if p.exists())
    cached = _REPOS.get(workspace)
    if cached and cached[0] == sig:
        return cached[1]
    sections = [s for p, r in files for s in _sections(p, r)]
    overview = _overview(workspace, files)
    if overview:
        sections.append(("workspace", f"{workspace} workspace overview — the repos and what each does", overview))
    idx = _Repos(sections)
    _REPOS[workspace] = (sig, idx)
    return idx


# --- his documents (the existing index) ----------------------------------------------

def _doc_vocabulary() -> set[str]:
    """The words his indexed documents use — what a misheard word is corrected to."""
    try:
        from . import store
        with store._connect() as conn:
            rows = conn.execute("SELECT body, document FROM knowledge_passages").fetchall()
        return {w for r in rows for w in _tokens(f"{r['document']} {r['body']}")}
    except Exception:                                          # noqa: BLE001
        return set()


_VOCAB: dict = {"words": None, "at": 0.0}


def _vocabulary(workspace: str) -> list[str]:
    if _VOCAB["words"] is None or time.time() - _VOCAB["at"] > REINDEX_SECONDS:
        words = set(_doc_vocabulary())
        repos = _repos(workspace)
        if repos:
            words |= set(repos.idf)
        _VOCAB.update(words=sorted(words), at=time.time())
    return _VOCAB["words"]


def corrected(question: str, workspace: str = "") -> list[str]:
    """His words, each misheard one replaced by the nearest word the knowledge
    uses: "telecos" → "telikos", "reprising" → "repricing"."""
    vocab = _vocabulary(workspace or default_workspace())
    known = set(vocab)
    out = []
    for w in _tokens(question):
        if w in known or not vocab:
            out.append(w)
            continue
        # Only a word the knowledge never uses is corrected, so the bar can be
        # where "telecos" (0.71) reaches "telikos".
        near = difflib.get_close_matches(w, vocab, n=1, cutoff=0.7)
        out.append(near[0] if near else w)
    return out


def _docs(words: list[str], limit: int) -> list[dict]:
    from . import knowledge
    if time.time() - _STATE["reindexed"] > REINDEX_SECONDS:
        _STATE["reindexed"] = time.time()
        try:
            knowledge.reindex()                 # only re-reads what changed on disk
        except Exception:                                      # noqa: BLE001
            pass
    question = " ".join(words)
    hits = knowledge.search(question, limit=limit * 2)
    # The code-checked flow document first when it matched.
    hits.sort(key=lambda h: 0 if FLOW_DOC.split(".")[0] in (h.get("path") or h.get("document") or "") else 1)
    # A third of what he named, not knowledge.relevant's half: "explain the
    # booking service, what it does" names three words, and one is generic.
    keep = [h for h in hits if knowledge.coverage(question, h) >= 0.34]
    return [{"source": "doc", "where": f"{h['document']} — {h['where']}", "text": h["text"],
             "score": h["score"]} for h in keep[:limit]]


# --- one lookup ----------------------------------------------------------------------

#: The flow document in his knowledge folder, checked against the code. His
#: corrections are written into it (its "Facts from Arun" section), not a side list,
#: and marked UNVERIFIED — his call, 2 Oct: "don't depend blindly on what I wrote
#: long back; depend on the code and the PDFs, reverify properly".
FLOW_DOC = "telikos-system-flow.md"
_FACTS = "## Facts from Arun"


def learn(said: str, restated: str = "", where: str = "voice") -> bool:
    """His correction or addition of a domain fact, written into the flow
    document marked unverified, read again at once."""
    from . import knowledge
    said = " ".join((said or "").split())
    if len(said) < 12:
        return False
    path = knowledge.folder() / FLOW_DOC
    try:
        text = path.read_text() if path.exists() else (
            "# Telikos system flow\n\n> Authoritative: wins over every other document.\n")
        if _FACTS not in text:
            text = text.rstrip() + f"\n\n{_FACTS}\n\nCorrections Arun gives are added here and win "\
                   "over anything above them.\n"
        line = (f"\n- {time.strftime('%Y-%m-%d')} ({where}) [Arun — unverified until checked "
                f"against the code]: {restated.strip() or said}")
        if restated.strip():
            line += f"  — in his words: \"{said[:300]}\""
        path.write_text(text.rstrip() + line + "\n")
        _STATE["reindexed"] = 0.0          # read again on the next lookup
        return True
    except OSError:
        return False


def default_workspace() -> str:
    try:
        from . import policy
        return policy.prefer("workspace") or "booking"
    except Exception:                                          # noqa: BLE001
        return "booking"


def workspace_for_root(root) -> str:
    """The registered name of the workspace at this path, or ''."""
    try:
        from .workspace import registry
        target = Path(root).resolve()
        for name in registry.all_workspaces():
            ws = registry.get(name)
            if ws is not None and Path(ws.root).resolve() == target:
                return name
    except Exception:                                          # noqa: BLE001
        pass
    return ""


def search(question: str, workspace: str = "", docs: int = 3, repos: int = 3) -> list[dict]:
    """His documents and the repo summaries, in turns, best first within each."""
    workspace = workspace or default_workspace()
    words = corrected(question, workspace)
    if not words:
        return []
    from_docs = _docs(words, docs)
    idx = _repos(workspace)
    from_code = idx.search(words, repos) if idx else []
    # Taken in turns — best document passage, best code summary, and so on — so
    # neither crowds the other out of the budget: "how does the email service
    # send the confirmation?" lost its answer (an email-service summary) to three
    # document passages about failures.
    found: list[dict] = []
    for i in range(max(len(from_docs), len(from_code))):
        found += from_docs[i:i + 1] + from_code[i:i + 1]
    return found


def render(hits: list[dict], budget: int = BUDGET_CHARS) -> str:
    """The passages as one block, never longer than `budget`."""
    out, used = [], 0
    for h in hits:
        where = "your docs" if h["source"] == "doc" else (
            "workspace" if h["source"] == "workspace" else f"{h['source']} (code summary)")
        piece = f"[{where}: {h['where']}]\n{h['text'].strip()}"
        if used + len(piece) > budget:
            room = max(0, budget - used)
            cut = piece[:room]
            piece = cut.rsplit("\n", 1)[0] if "\n" in cut[room // 2:] else cut.rsplit(" ", 1)[0]
            if len(piece) < 120:
                break
        out.append(piece)
        used += len(piece) + 2
    return "\n\n".join(out)


def lookup(question: str, workspace: str = "", budget: int = BUDGET_CHARS, channel: str = "") -> str:
    """The best few passages for this question, or ''. Every lookup is logged —
    question, what was found, how much went out — so a question it misses is
    seen, not guessed at."""
    hits = search(question, workspace)
    out = render(hits, budget)
    try:
        from . import store
        docs = sum(1 for h in hits if h["source"] == "doc")
        store.record_outcome("knowledge", "lookup", subject=channel or "",
                             detail=f"{docs} doc + {len(hits) - docs} code · {len(out)} chars · "
                                    f"{(question or '')[:120]}")
    except Exception:                                          # noqa: BLE001
        pass
    return out


#: Questions the knowledge answers: what something is, how it works, why.
KNOWLEDGE_ASK = re.compile(
    r"\b(?:what(?:'s| is| are| does)|explain|how does|how do|how is|tell (?:me |us )?(?:about|abt)|"
    r"walk me through|know about|brief (?:me|on|about)|"
    r"which (?:system|service|team)|who owns|what happens|meaning of|describe|overview|flow)\b", re.I)


def is_knowledge_question(text: str) -> bool:
    return bool(KNOWLEDGE_ASK.search(text or ""))
