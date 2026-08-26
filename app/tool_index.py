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


# --- intent tier -------------------------------------------------------------
# The biggest token lever isn't narrowing the toolset — it's not sending tools at
# all on turns that don't need them. A large share of chat is pure reasoning ABOUT
# the conversation ("explain that", "why", "elaborate", "summarise the above"):
# it needs the model but no capability, yet it ranks no tool and so used to fall
# into the "expose everything" default and pay the full schema. These get the
# ALWAYS floor only.
#
# The gate is deliberately TIGHT and doubled — the opener must be unmistakably
# conversational AND the ranker must have found nothing — because a false positive
# strands a real request (the floor has no teams/jira/file tools), which is a far
# worse failure than a few wasted tokens.
_CONVERSATIONAL = re.compile(
    r"^\s*(?:"
    r"explain|elaborate|clarify|rephrase|reword|summari[sz]e|recap|expand|"
    r"describe|define|compare|contrast|"
    r"why\b|why'?s\b|how\s+come|what\s+do\s+you\s+mean|what\s+did\s+you\s+mean|"
    r"what\s+does\s+(?:that|this|it)\s+mean|what'?s\s+the\s+difference|"
    r"tell\s+me\s+more|tell\s+me\s+about|go\s+on|"
    r"walk\s+me\s+through|break\s+(?:that|this|it)\s+down|"
    r"help\s+me\s+understand|i\s+don'?t\s+(?:get|understand)|"
    r"(?:can|could)\s+you\s+explain|give\s+me\s+more\s+detail|more\s+detail|"
    r"in\s+short|tl;?dr|what\s+do\s+you\s+think|your\s+(?:opinion|take|thoughts)"
    r")\b", re.I)


def is_conversational(text: str) -> bool:
    """A pure reasoning/explanation turn about the conversation itself — needs the
    model, no external capability. Kept tight on purpose (see the note above)."""
    return bool(_CONVERSATIONAL.match((text or "").strip()))


def _floor() -> list[str]:
    """Just the ALWAYS core — the Tier-0 toolset."""
    reg = capabilities.registry()
    return sorted(n for n in capabilities.ALWAYS if n in reg)


def select_sticky(conv_id: str, query: str, k: int = TOP_K) -> list[str] | None:
    picked = select(query, k)
    # Tier 0: nothing ranked AND clearly conversational -> the floor only, not all
    # 50 schemas. Transient — it does not grow the conversation's sticky set, so a
    # later real request re-picks cleanly.
    if picked is None and is_conversational(query):
        return _floor()
    if not conv_id:
        return picked
    prev = _sticky.get(conv_id)
    if picked is None:
        # This turn wants everything; so does the rest of the conversation.
        _sticky.pop(conv_id, None)
        _sticky[conv_id] = dict.fromkeys(capabilities.registry())
        return None
    # Bounded by RECENCY, not accumulated forever. Stickiness exists so a
    # follow-up with no keywords in it ("do that again", "and the other repo")
    # still has the right tools — but the old set was monotonic: it only ever
    # grew. Measured on six ordinary turns it went 23 -> 26 -> 31 -> 36 -> 37 ->
    # 44 of 58, and on reaching 56 it latched to "everything" for the rest of the
    # conversation. So the longer Arun talked to Asta, the more every turn cost,
    # and it never recovered — with ~6,000 tokens of schemas as the ceiling.
    #
    # Keeping the most recently useful tools bounds that permanently. A tool that
    # drops out is not lost: naming its subject re-selects it immediately.
    merged = _recent(prev, picked)
    if len(merged) >= len(capabilities.registry()) - 2:
        _sticky[conv_id] = dict.fromkeys(capabilities.registry())
        return None
    if len(_sticky) >= _STICKY_MAX and conv_id not in _sticky:
        _sticky.clear()
    _sticky[conv_id] = merged
    return sorted(merged)


#: How many tools a conversation may carry between turns. Eight are picked per
#: turn, so this is roughly three turns of memory plus the floor — enough for a
#: follow-up, far short of the whole registry.
STICKY_MAX_TOOLS = int(os.environ.get("ASTA_TOOL_STICKY_MAX", "24"))


#: Eviction only starts above this, and then trims back to STICKY_MAX_TOOLS.
#: Without the gap the set sits exactly at the cap and every turn evicts
#: something to make room — measured as one tool in and one out on two
#: consecutive turns about the SAME subject, which changes the tool block and
#: therefore misses the prompt cache on a turn that should have hit it. With the
#: gap, trimming happens about once every eight new tools instead of every turn.
STICKY_SLACK = int(os.environ.get("ASTA_TOOL_STICKY_SLACK", "8"))


def _recent(prev, picked: list[str]) -> dict:
    """This turn's tools and the floor, then whatever else still fits.

    Two sets are NOT negotiable, and saying so in a comment was not enough. The
    floor used to be appended last and the trim kept the first N — so the one
    group documented as "never evicted" was the first group evicted. It stayed
    invisible while the registry was small enough that nothing ever trimmed;
    adding three capabilities crossed the threshold and a test caught it.

    What that bug actually costs: the floor is `capabilities.ALWAYS` — ask_user,
    continue_working, load_skill, remember, and **prepare_to_send**. A long
    conversation would quietly lose the staged-send gate, so the one hard rule in
    the system — nothing goes out without being staged first — would have been
    enforced by a tool the model could no longer reach.

    So: this turn's picks and the floor are kept whatever the cap says, and only
    the carried-over tools compete for what room is left.
    """
    # Order matters for the next turn's recency, so build it deliberately:
    # what this turn asked for, then the always-core, then history.
    protected: dict = dict.fromkeys(list(picked) + _floor())
    order: dict = dict(protected)
    for name in (prev or {}):
        order.setdefault(name, None)
    if len(order) <= STICKY_MAX_TOOLS + STICKY_SLACK:
        return order
    room = max(0, STICKY_MAX_TOOLS - len(protected))
    carried = [n for n in order if n not in protected]
    return dict.fromkeys(list(protected) + carried[:room])


def forget(conv_id: str) -> None:
    """Drop a conversation's toolset — used when its session is rotated."""
    _sticky.pop(conv_id, None)
    _mcp_sticky.pop(conv_id, None)


# --- MCP servers, which were never narrowed at all ---------------------------
#
# Native tools have been retrieved per turn for a long time. The MCP toolsets
# were not: `toolsets=MCP_TOOLSETS or None` attached every server to every turn,
# whatever the turn was about. Measured on this machine — 32 tools, ~6,205 tokens
# per turn, and that is WITHOUT Grafana, which failed to enumerate. GitHub alone
# is 26 tools and ~3,917 tokens, present whether or not GitHub was mentioned.
#
# That is more than the entire native tool surface after narrowing, so it is the
# larger half of the prompt floor and none of it was being retrieved.

#: server -> what a turn looks like when it needs that server. Deliberately
#: generous: attaching a server that turns out to be unnecessary costs tokens,
#: while missing one costs a capability, and the sticky set below means one
#: mention keeps it for the rest of the conversation.
MCP_TRIGGERS: dict[str, tuple[str, ...]] = {
    "grafana": ("grafana", "log", "logs", "metric", "latency", "error rate", "dashboard",
                "loki", "prometheus", "tempo", "trace", "traces", "span", "alert",
                "5xx", "exception", "stack trace", "throughput", "p99", "outage",
                "slow", "spike", "crash", "oom", "restart", "why is", "what happened"),
    "temporal": ("temporal", "workflow", "workflows", "activity", "activities",
                 "execution", "signal", "task queue", "retry", "saga", "stuck",
                 "booking id", "servicePlanNumber", "trace the booking"),
    "github": ("github", "pr", "pull request", "review", "merge", "branch", "commit",
               "issue", "repo", "repository", "diff", "ci", "workflow run", "checks"),
    "atlassian": ("jira", "ticket", "sprint", "backlog", "epic", "story", "bug",
                  "beptelikos", "assigned to me", "board", "confluence"),
    "context7": ("docs", "documentation", "api reference", "library", "framework",
                 "how do i use", "spring", "mapstruct", "reactor", "webflux"),
}

_mcp_sticky: dict[str, set[str]] = {}


def mcp_for(conv_id: str, query: str) -> set[str] | None:
    """Which MCP servers this turn needs. None means "all of them".

    Sticky for the same reason the native selection is: a follow-up like "and the
    one before that" has no keyword in it, and losing Grafana halfway through a
    debugging conversation is worse than carrying it.
    """
    if not enabled():
        return None
    text = (query or "").lower()
    # Word boundaries, not substrings. "priya" contains "pr", so drafting a mail
    # to a colleague pulled in twenty-six GitHub tools — the exact waste this is
    # meant to remove, caused by the mechanism meant to remove it.
    hit = {name for name, words in MCP_TRIGGERS.items()
           if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text) for w in words)}
    if not conv_id:
        return hit or set()
    prev = _mcp_sticky.get(conv_id, set())
    merged = prev | hit
    if len(_mcp_sticky) >= _STICKY_MAX and conv_id not in _mcp_sticky:
        _mcp_sticky.clear()
    _mcp_sticky[conv_id] = merged
    return merged


def forget_mcp(conv_id: str) -> None:
    _mcp_sticky.pop(conv_id, None)
