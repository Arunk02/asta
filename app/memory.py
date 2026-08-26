"""Memory engine: markdown facts/episodes as source of truth, SQLite FTS5 as index.

Layout (under asta/memory/):
  MEMORY.md     - tiny index, always injected into the system prompt
  facts/*.md    - one durable fact per file (prefs, gotchas, project facts)
  episodes/*.md - end-of-session digests

Background jobs (digests, consolidation, compaction summaries) run on the
local LM Studio model when reachable, so they cost zero API tokens.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import sys
from pathlib import Path

import httpx

from . import store

ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = ROOT / "memory"
FACTS_DIR = MEMORY_DIR / "facts"
EPISODES_DIR = MEMORY_DIR / "episodes"
INDEX_FILE = MEMORY_DIR / "MEMORY.md"

INDEX_HEADER = "# Asta Memory Index\n\nOne line per memory. Full facts live in facts/ and episodes/.\n\n"
INDEX_MAX_CHARS = 4000  # keep the always-in-prompt index tiny


def ensure_dirs() -> None:
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_FILE.exists():
        INDEX_FILE.write_text(INDEX_HEADER)


def slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len] or "note"


# --- local model (free background brain) -------------------------------------

def local_llm_base() -> str:
    return os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").rstrip("/")


def local_llm_model() -> str | None:
    """First loaded model in LM Studio, or None if unreachable."""
    try:
        r = httpx.get(f"{local_llm_base()}/models", timeout=2)
        data = r.json().get("data", [])
        return data[0]["id"] if data else None
    except Exception:
        return None


def local_llm_complete(prompt: str, max_tokens: int = 400) -> str | None:
    model = local_llm_model()
    if not model:
        return None
    try:
        r = httpx.post(
            f"{local_llm_base()}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
            timeout=120,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


async def cheap_complete(prompt: str, max_tokens: int = 400,
                         paid_ok: bool = False) -> str | None:
    """One short completion, free if possible, from whichever brain is actually up.

    Nine background features called `local_llm_complete` directly and treated None
    as "produce nothing" — so with LM Studio closed, meeting prep shipped an empty
    checklist, recaps and Teams drafts came back blank, and the learning loop
    quietly stopped learning. "Free-first" was the right instinct; "free-only" was
    an accident of it, and it made the whole background half of Asta depend on one
    optional local process nobody is told to keep running.

    Order is cheapest-first and stops at the first brain that answers: LM Studio
    (free), then an API key if there is one, then a CLI subscription. The last two
    are gated on `paid_ok`, because the caller knows whether this is worth money —
    prep for a meeting Arun walks into in half an hour is; re-digesting yesterday's
    chat at 2am is not, and that one should just skip a night.

    Returns None when nothing could answer, and callers must treat that as "say
    nothing" rather than "emit a template".
    """
    out = await asyncio.to_thread(local_llm_complete, prompt, max_tokens)
    if (out or "").strip():
        return out.strip()
    if not paid_ok:
        return None
    from . import agent as agent_mod
    try:
        name = agent_mod.best_model_name()
    except RuntimeError:
        name = ""
    if name:
        try:
            from pydantic_ai import Agent as _Agent
            result = await _Agent(model=agent_mod.get_model(name)).run(prompt)
            if (result.output or "").strip():
                return result.output.strip()
        except Exception as exc:                       # noqa: BLE001
            # Same durable marking as the in-call path: a 401 means this brain is
            # out of service until the key changes, and every caller after this
            # one should go straight to a CLI subscription instead of paying for
            # the same refusal again.
            if agent_mod.credential_failure(str(exc)):
                agent_mod.mark_key_rejected(name, str(exc))
    # Last resort: a CLI subscription Arun already pays for. Bounded to one short
    # turn — this is a paragraph of prep, not a task.
    for cli_name in agent_mod.EXECUTORS:
        if not agent_mod.available(cli_name):
            continue
        try:
            text = await agent_mod.runner(cli_name).one_shot(prompt, timeout=120)
            if (text or "").strip():
                return text.strip()
        except Exception:
            continue
    return None


# --- learning from corrections -----------------------------------------------
#
# The gap this closes: `remember` only ever fired when Arun said "remember this"
# or the model chose to call it — so after weeks of use exactly ONE durable fact
# existed against 18 episodes. Every correction he gave evaporated with the
# conversation, and the same mistake came back a week later.
#
# A correction is the single highest-value thing to store: it is Arun stating a
# preference or a fact the assistant demonstrably did not have. Detecting it is
# free (regex over his own message), and the write happens off the turn so it
# never slows a reply.

_CORRECTION = re.compile(
    r"^\s*(no+\b|nope|wrong\b|that'?s wrong|not (right|correct|what)\b|incorrect)"
    r"|\bi (already )?told you\b|\bi said\b|\byou (keep|always|again)\b"
    r"|\bwhy (did|are) you\b|\bdon'?t (do|use|add|send|call)\b"
    r"|\bnever (do|use|add|send|say|post|reply|commit|push|call)\b"
    r"|\bstop (doing|using|adding|sending|posting)\b"
    r"|\bnot like that\b|\bthat'?s not\b|\bshould(n'?t| not) have\b"
    r"|\bactually,? (it|the|that|no)\b|\bcorrection\b", re.I)

# Phrasings that look like corrections but are just conversation.
_NOT_CORRECTION = re.compile(r"^\s*(no worries|no problem|no need|nope,? all good)\b", re.I)


def looks_like_correction(text: str) -> bool:
    t = (text or "").strip()
    if not t or _NOT_CORRECTION.search(t):
        return False
    return bool(_CORRECTION.search(t))


def learn_from_correction(conv_id: str, user_text: str) -> str | None:
    """Turn 'no, not like that' into a durable gotcha.

    Pairs Arun's correction with what the assistant had just said, so the stored
    lesson has both halves — a correction without its context is unusable later.
    Phrased by the LOCAL model (free); falls back to storing the pair verbatim
    when LM Studio is down, because a slightly clumsy memory beats no memory.
    """
    if not looks_like_correction(user_text):
        return None
    prior = ""
    try:
        # No slicing: this runs BEFORE the correction is stored, so the newest
        # row is still the assistant reply being corrected.
        msgs = store.list_ui_messages(conv_id)
        for m in reversed(msgs or []):
            if m.get("role") == "assistant" and (m.get("content") or "").strip():
                prior = (m["content"] or "")[:800]
                break
    except Exception:
        pass
    if not prior:
        return None

    phrased = local_llm_complete(
        "Arun corrected his assistant. Write ONE durable lesson so the mistake is "
        "not repeated. Two lines exactly:\n"
        "TITLE: <6 words max>\n"
        "LESSON: <one sentence: what to do instead, stated as a rule>\n"
        "No preamble, no markdown.\n\n"
        f"Assistant had said:\n{prior}\n\nArun's correction:\n{user_text[:400]}",
        max_tokens=120,
    )
    title, lesson = "", ""
    for line in (phrased or "").splitlines():
        if line.upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()[:60]
        elif line.upper().startswith("LESSON:"):
            lesson = line.split(":", 1)[1].strip()
    if not lesson:
        title = f"Correction: {user_text.strip()[:40]}"
        lesson = (f"Arun corrected this. He said: “{user_text.strip()[:200]}”\n\n"
                  f"It had just said: “{prior[:300]}”")
    return remember(title or "Correction", lesson, "gotcha")


# --- write path --------------------------------------------------------------

def remember(title: str, fact: str, mtype: str = "fact") -> str:
    """Persist a durable fact. mtype: fact | preference | gotcha | fix."""
    ensure_dirs()
    slug = slugify(title)
    path = FACTS_DIR / f"{slug}.md"
    today = dt.date.today().isoformat()
    path.write_text(
        f"---\nname: {slug}\ntitle: {title}\ntype: {mtype}\ndate: {today}\n---\n\n{fact.strip()}\n"
    )
    _index_add_line(f"- [{title}](facts/{slug}.md) — {mtype}")
    reindex()
    return str(path.relative_to(MEMORY_DIR))


def write_episode(conv: dict) -> str | None:
    """Digest a finished conversation into episodes/. Local model first, heuristic fallback."""
    ensure_dirs()
    msgs = store.list_ui_messages(conv["id"])
    if not msgs:
        return None
    transcript = "\n".join(f"{m['role']}: {m['content'][:800]}" for m in msgs)[-8000:]
    digest = local_llm_complete(
        "Summarize this dev-assistant chat session in at most 10 short lines of markdown. "
        "Focus on: what was asked, what was done/decided, and anything reusable next time "
        "(root causes, fixes, preferences). No preamble.\n\n" + transcript
    )
    if not digest:
        first = next((m["content"] for m in msgs if m["role"] == "user"), "")[:300]
        last = next((m["content"] for m in reversed(msgs) if m["role"] == "assistant"), "")[:500]
        digest = f"**Asked:** {first}\n\n**Outcome:** {last}"
    today = dt.date.today().isoformat()
    slug = slugify(conv.get("title") or "session")
    path = EPISODES_DIR / f"{today}-{slug}-{conv['id'][:6]}.md"
    path.write_text(
        f"---\nname: {path.stem}\ntitle: {conv.get('title') or 'Session'}\ntype: episode\ndate: {today}\n---\n\n{digest}\n"
    )
    store.update_conversation(conv["id"], digested=1)
    reindex()
    return str(path.relative_to(MEMORY_DIR))


def _index_add_line(line: str) -> None:
    text = INDEX_FILE.read_text() if INDEX_FILE.exists() else INDEX_HEADER
    if line not in text:
        INDEX_FILE.write_text(text.rstrip() + "\n" + line + "\n")


# --- read path ---------------------------------------------------------------

def index_text() -> str:
    ensure_dirs()
    return INDEX_FILE.read_text()[:INDEX_MAX_CHARS]


def local_embed(texts: list[str]) -> list[list[float]] | None:
    """Embeddings from LM Studio — local and free. None when it isn't running."""
    model = os.environ.get("ASTA_EMBED_MODEL", "").strip() or local_llm_model()
    if not model or not texts:
        return None
    try:
        r = httpx.post(f"{local_llm_base()}/embeddings",
                       json={"model": model, "input": texts}, timeout=30)
        data = r.json().get("data")
        if not data or len(data) != len(texts):
            return None
        return [d["embedding"] for d in data]
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / (na * nb) if na and nb else 0.0


def _age_days(date_str: str) -> float:
    try:
        return max(0.0, (dt.date.today() - dt.date.fromisoformat(date_str)).days)
    except (ValueError, TypeError):
        return 90.0


# Half-life in days: a lesson from March should not outrank yesterday's on a tie.
RECENCY_HALFLIFE = float(os.environ.get("ASTA_RECALL_HALFLIFE", "45"))

# A memory must be at least this semantically close to the query to be surfaced.
# Below it, "recall" was injecting whatever FTS happened to return and labelling
# it relevant — which is how a question about a Teams send failure got answered
# with a ten-day-old document-storage note. An unrelated memory is worse than no
# memory: it doesn't just fail to help, it actively misdirects the answer.
RECALL_FLOOR = float(os.environ.get("ASTA_RECALL_FLOOR", "0.35"))


def recall(query: str, k: int = 4) -> list[dict]:
    """Top-k memories that are actually about the query — or nothing.

    FTS5 alone is keyword-only: "what breaks the grafana proxy" misses a fact
    worded "VPN required for monitoring". So FTS casts a WIDE net (cheap) and
    embeddings re-rank it (local, free), with a recency weight to separate ties
    and a relevance FLOOR so coincidental matches are dropped rather than shown.

    When LM Studio is down there is no embedder to judge meaning, so we cannot
    tell a real match from a keyword collision. That is the riskiest moment, not
    a free pass: the stopword filter has already removed generic terms, so a
    surviving FTS hit is at least on a real topic — take only the few best, and
    lean on the caller framing recall as "ignore if not relevant".
    """
    hits = store.memory_search(query, max(k * 4, 12))
    if not hits:
        return []
    vecs = local_embed([query] + [f"{h['title']} {h.get('snippet', '')}" for h in hits])
    if not vecs:
        return hits[:min(k, 3)]
    qv, hvs = vecs[0], vecs[1:]
    scored = []
    for h, hv in zip(hits, hvs):
        sim = _cosine(qv, hv)
        if sim < RECALL_FLOOR:
            continue                     # unrelated — don't surface it at all
        recency = 0.5 ** (_age_days(h.get("date", "")) / RECENCY_HALFLIFE)
        # Meaning dominates; recency only separates near-ties, and can never lift
        # a below-floor memory over the bar (the floor is checked on sim alone).
        scored.append((sim + 0.15 * recency, h))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [h for _, h in scored[:k]]


def recall_block(query: str) -> str:
    hits = recall(query)
    if not hits:
        return ""
    lines = [f"- **{h['title']}** ({h['mtype']}): {h['snippet']}" for h in hits]
    # NOT "relevant memories" — that framing is what made the model trust a
    # marginal hit and answer from it. These are candidates; the model must judge
    # each against the actual question and drop the ones that don't bear on it.
    return ("Notes pulled from memory that MIGHT relate — use one only if it clearly "
            "answers the question at hand, otherwise ignore it (do not force a "
            "connection):\n" + "\n".join(lines))


def read_memory_file(rel_path: str) -> str | None:
    path = (MEMORY_DIR / rel_path).resolve()
    if not str(path).startswith(str(MEMORY_DIR)) or not path.is_file():
        return None
    return path.read_text()


def list_memories() -> list[dict]:
    ensure_dirs()
    out = []
    for d, mtype in ((FACTS_DIR, "fact"), (EPISODES_DIR, "episode")):
        for p in sorted(d.glob("*.md")):
            meta = _parse_frontmatter(p.read_text())
            out.append({
                "path": str(p.relative_to(MEMORY_DIR)),
                "title": meta.get("title", p.stem),
                "type": meta.get("type", mtype),
                "date": meta.get("date", ""),
            })
    return out


def _parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return meta


def _body(text: str) -> str:
    return re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL).strip()


# --- index maintenance -------------------------------------------------------

def reindex() -> int:
    """Rebuild the FTS index from the markdown files (cheap; always in sync)."""
    ensure_dirs()
    docs = []
    for d in (FACTS_DIR, EPISODES_DIR):
        for p in d.glob("*.md"):
            text = p.read_text()
            meta = _parse_frontmatter(text)
            docs.append({
                "path": str(p.relative_to(MEMORY_DIR)),
                "title": meta.get("title", p.stem),
                "mtype": meta.get("type", "fact"),
                "body": _body(text),
                "date": meta.get("date", ""),      # drives recency weighting
            })
    store.memory_reindex(docs)
    return len(docs)


def consolidate() -> str:
    """Nightly job: digest stale sessions, dedupe facts, rewrite MEMORY.md, reindex."""
    ensure_dirs()
    report = []

    for conv in store.stale_undigested_conversations(idle_seconds=0):
        p = write_episode(conv)
        if p:
            report.append(f"digested session {conv['id']} -> {p}")

    # Merge facts whose titles are near-duplicates (same slug prefix).
    seen: dict[str, Path] = {}
    for p in sorted(FACTS_DIR.glob("*.md")):
        key = p.stem[:32]
        if key in seen:
            keeper = seen[key]
            merged = _body(keeper.read_text()) + "\n\n" + _body(p.read_text())
            summary = local_llm_complete(
                "Merge these overlapping notes into one concise note (keep every distinct fact, "
                "drop repetition, max 8 lines):\n\n" + merged,
                max_tokens=300,
            )
            keeper_meta = _parse_frontmatter(keeper.read_text())
            head = re.match(r"^---\n.*?\n---\n", keeper.read_text(), re.DOTALL).group(0)
            keeper.write_text(head + "\n" + (summary or merged) + "\n")
            p.unlink()
            report.append(f"merged {p.name} into {keeper.name} ({keeper_meta.get('title')})")
        else:
            seen[key] = p

    # Keep only the most recent 30 episodes.
    episodes = sorted(EPISODES_DIR.glob("*.md"))
    for p in episodes[:-30]:
        p.unlink()
        report.append(f"pruned old episode {p.name}")

    # Rewrite MEMORY.md from surviving files.
    lines = [INDEX_HEADER.rstrip(), ""]
    for m in list_memories():
        if m["type"] != "episode":
            lines.append(f"- [{m['title']}]({m['path']}) — {m['type']}")
    recent = [m for m in list_memories() if m["type"] == "episode"][-7:]
    if recent:
        lines.append("\n## Recent sessions")
        lines += [f"- [{m['title']}]({m['path']}) — {m['date']}" for m in recent]
    INDEX_FILE.write_text("\n".join(lines) + "\n")

    n = reindex()
    report.append(f"reindexed {n} memory docs")
    return "\n".join(report) if report else "nothing to do"


# --- long-chat compaction ----------------------------------------------------

COMPACT_AFTER_MSGS = 30   # ui messages before we compact
KEEP_RECENT_MSGS = 16     # model messages kept verbatim


def compact_summary(conv: dict) -> str | None:
    """Summarize older turns so the model only replays recent ones verbatim."""
    msgs = store.list_ui_messages(conv["id"])
    if len(msgs) <= COMPACT_AFTER_MSGS:
        return None
    older = msgs[:-KEEP_RECENT_MSGS]
    transcript = "\n".join(f"{m['role']}: {m['content'][:500]}" for m in older)[-6000:]
    summary = local_llm_complete(
        "Compress this earlier part of a chat into at most 12 lines the assistant needs to "
        "continue the conversation coherently (decisions, facts, open threads). No preamble.\n\n"
        + transcript,
        max_tokens=350,
    )
    if not summary:
        summary = "\n".join(f"{m['role']}: {m['content'][:120]}" for m in older[-12:])
    return summary


if __name__ == "__main__":
    store.init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "reindex"
    if cmd == "consolidate":
        print(consolidate())
    elif cmd == "reindex":
        print(f"reindexed {reindex()} docs")
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
