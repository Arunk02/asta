"""Pick the tools this message actually needs.

Every turn used to carry all 32 tool schemas — the largest fixed cost in the
prompt, and one that grows with every capability added. This ranks capabilities
against the message and exposes the top few plus a small always-available core,
so adding the thirty-third tool costs nothing on turns that don't need it.

Two rankers, in order:

  1. Embeddings from LM Studio (local, free). Descriptions change rarely, so the
     vectors are cached on disk against a fingerprint of the descriptions and
     recomputed only when a capability's wording actually changes.
  2. Lexical overlap, when LM Studio isn't running. Weaker, but it is the common
     case on a laptop and it beats the alternative.

The safety rule throughout: when ranking is uncertain — no signal, too few
matches, anything unexpected — return None, meaning "expose everything". A turn
that costs more is a much smaller failure than a turn that cannot reach the tool
it needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import capabilities, memory

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "tool_index.json"

#: How many ranked capabilities to expose, before the ALWAYS core is added.
TOP_K = int(os.environ.get("ASTA_TOOL_TOP_K", "8"))

#: Below this cosine score a hit is noise, not a match.
MIN_SCORE = float(os.environ.get("ASTA_TOOL_MIN_SCORE", "0.25"))

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was",
    "what", "which", "who", "how", "why", "when", "do", "does", "did", "can", "you",
    "me", "my", "i", "it", "that", "this", "with", "from", "at", "be", "have", "has",
    "any", "all", "get", "please", "just", "now", "then", "if", "so", "up", "out",
}

_WORD = re.compile(r"[a-z0-9_]+")


def enabled() -> bool:
    """Off by default is wrong here — but so is silently narrowing a toolset the
    user is debugging. ASTA_TOOL_RAG=0 restores the all-tools behaviour."""
    return os.environ.get("ASTA_TOOL_RAG", "1").strip().lower() not in ("0", "false", "no")


def _docs() -> dict[str, str]:
    """name -> the text we rank against: the name, its group, and its summary."""
    return {c.name: f"{c.name} ({c.group}): {c.summary}"
            for c in capabilities.registry().values()}


def _fingerprint(docs: dict[str, str]) -> str:
    blob = "\x00".join(f"{k}={v}" for k, v in sorted(docs.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _load_cache(fp: str) -> dict[str, list[float]] | None:
    try:
        data = json.loads(CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("vectors") if data.get("fingerprint") == fp else None


def _save_cache(fp: str, vectors: dict[str, list[float]]) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"fingerprint": fp, "vectors": vectors}))
    except OSError:
        pass   # a cold cache costs one embedding call, not correctness


def _vectors(docs: dict[str, str]) -> dict[str, list[float]] | None:
    fp = _fingerprint(docs)
    cached = _load_cache(fp)
    if cached and set(cached) == set(docs):
        return cached
    names = list(docs)
    embedded = memory.local_embed([docs[n] for n in names])
    if not embedded:
        return None
    vectors = dict(zip(names, embedded))
    _save_cache(fp, vectors)
    return vectors


#: Crudest possible stemmer, and deliberately so: it exists to make "remind"
#: match "reminder" and "message" match "messages". Anything cleverer would need
#: a dependency to serve a fallback path that only runs when LM Studio is off.
_SUFFIXES = ("ers", "ing", "ers", "er", "es", "ed", "s")


def _stem(word: str) -> str:
    for suf in _SUFFIXES:
        if len(word) > len(suf) + 2 and word.endswith(suf):
            word = word[: -len(suf)]
            break
    # A trailing 'e' goes too, or "message" and "messages" stem to different
    # things ("message" vs "messag") and never match each other — which is
    # exactly the pair this stemmer exists for.
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def _tokens(text: str) -> set[str]:
    words: list[str] = []
    for raw in _WORD.findall((text or "").lower()):
        # Tool names are snake_case, so "ci_status" must also answer to "ci" —
        # otherwise the one term a user actually types never matches the tool.
        words += [raw, *raw.split("_")] if "_" in raw else [raw]
    return {_stem(w) for w in words if len(w) > 1 and w not in _STOP}


def _lexical_scores(query: str, docs: dict[str, str]) -> dict[str, float]:
    """Overlap between the message and each description, normalised by query size.

    Rare words carry more: a message mentioning "jira" should pull the Jira group
    much harder than one mentioning "task", which appears in half the table.
    """
    q = _tokens(query)
    if not q:
        return {}
    doc_tokens = {n: _tokens(t) for n, t in docs.items()}
    freq: dict[str, int] = {}
    for toks in doc_tokens.values():
        for t in toks:
            freq[t] = freq.get(t, 0) + 1
    total = len(doc_tokens) or 1
    scores: dict[str, float] = {}
    for name, toks in doc_tokens.items():
        hit = q & toks
        if not hit:
            continue
        # 1/frequency: a term in one description is worth far more than one in twenty.
        scores[name] = sum(1.0 / freq.get(t, 1) for t in hit) / len(q) * total / 4
    return scores


def _embed_scores(query: str, docs: dict[str, str]) -> dict[str, float] | None:
    vectors = _vectors(docs)
    if not vectors:
        return None
    qv = memory.local_embed([query])
    if not qv:
        return None
    return {n: memory._cosine(qv[0], v) for n, v in vectors.items()}


def rank(query: str) -> list[tuple[str, float]]:
    """(name, score) best first. Empty when nothing scored — callers treat that
    as 'no opinion', not as 'no tools'."""
    docs = _docs()
    scores = _embed_scores(query, docs)
    if scores is None:
        scores = _lexical_scores(query, docs)
    return sorted(((n, s) for n, s in scores.items() if s >= MIN_SCORE),
                  key=lambda kv: kv[1], reverse=True)


def select(query: str, k: int = TOP_K) -> list[str] | None:
    """Capability names for this message, or None meaning 'expose everything'.

    The whole group of the top hit comes along: half a group is a trap — a model
    that can read a Jira issue but cannot comment on it will improvise something
    worse than asking.
    """
    if not enabled() or not (query or "").strip():
        return None
    ranked = rank(query)
    if not ranked:
        return None
    reg = capabilities.registry()
    chosen: list[str] = [n for n, _ in ranked[:k]]
    top_group = reg[ranked[0][0]].group
    chosen += [n for n, c in reg.items() if c.group == top_group and n not in chosen]
    chosen += [n for n in capabilities.ALWAYS if n in reg and n not in chosen]
    # Narrowing to nearly-everything saves nothing and risks dropping the one
    # tool that mattered — hand back None and keep the simple path.
    return None if len(chosen) >= len(reg) - 2 else chosen


# --- per-conversation stickiness ---------------------------------------------
# Tool definitions sit in the cached prompt prefix, so changing the toolset
# mid-conversation costs a cache miss. Selecting fresh every turn would trade a
# fixed cost for a recurring one.
#
# So a conversation's toolset only ever GROWS: turn one picks, later turns add
# what they need. Each addition costs one miss; a conversation that stays on
# topic costs none, and one that wanders ends up where it would have started
# anyway. In-process only — a restart re-picks, which is correct, since history
# is re-read from scratch too.
_sticky: dict[str, set[str]] = {}
_STICKY_MAX = 64


def select_sticky(conv_id: str, query: str, k: int = TOP_K) -> list[str] | None:
    picked = select(query, k)
    if not conv_id:
        return picked
    prev = _sticky.get(conv_id)
    if picked is None:
        # This turn wants everything; so does the rest of the conversation.
        _sticky.pop(conv_id, None)
        _sticky[conv_id] = set(capabilities.registry())
        return None
    merged = (prev or set()) | set(picked)
    if len(merged) >= len(capabilities.registry()) - 2:
        _sticky[conv_id] = set(capabilities.registry())
        return None
    if len(_sticky) >= _STICKY_MAX and conv_id not in _sticky:
        _sticky.clear()
    _sticky[conv_id] = merged
    return sorted(merged)


def forget(conv_id: str) -> None:
    """Drop a conversation's toolset — used when its session is rotated."""
    _sticky.pop(conv_id, None)
