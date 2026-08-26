"""Tool selection: fewer tools per turn, without ever hiding the one that mattered."""

from __future__ import annotations

import pytest

from app import capabilities, tool_index


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    """Force the lexical path. LM Studio may or may not be running on this
    machine, and a test whose ranker depends on that proves nothing."""
    monkeypatch.setattr("app.memory.local_embed", lambda texts: None)
    tool_index._sticky.clear()


def _sel(query):
    return tool_index.select(query)


def test_selection_is_actually_narrower():
    picked = _sel("any messages from Vinish?")
    assert picked is not None
    assert len(picked) < len(capabilities.registry()) / 2


@pytest.mark.parametrize("query,expected", [
    ("any messages from Vinish?", "teams_activity"),
    ("comment on ABC-123 that it's done", "jira_comment"),
    ("remind me to call the team at 5pm", "set_reminder"),
    ("what is broken right now", "health_check"),
    ("draft my standup", "standup_draft"),
])
def test_the_obvious_tool_is_present(query, expected):
    picked = _sel(query)
    assert picked is None or expected in picked, f"{query!r} lost {expected}"


def test_always_set_survives_every_selection():
    for query in ("any messages", "jira ABC-1", "what is broken"):
        picked = _sel(query)
        if picked is not None:
            assert set(capabilities.ALWAYS) <= set(picked), query


def test_whole_group_comes_along():
    """Half a group is a trap: a model that can read a Jira issue but not comment
    on it improvises something worse than asking."""
    picked = _sel("comment on ABC-123 that it's done")
    assert picked is not None
    jira = {n for n, c in capabilities.registry().items() if c.group == "jira"}
    assert jira <= set(picked)


def test_no_signal_means_everything_not_nothing():
    assert _sel("") is None
    assert _sel("hmm ok") is None


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("ASTA_TOOL_RAG", "0")
    assert _sel("any messages from Vinish") is None


@pytest.mark.parametrize("a,b", [
    ("remind", "reminder"), ("message", "messages"), ("match", "matches"),
    ("service", "services"),
])
def test_word_forms_stem_together(a, b):
    """Singular and plural must land on the same token or they never match."""
    assert tool_index._tokens(a) == tool_index._tokens(b)


def test_snake_case_names_answer_to_their_parts():
    """'CI' must reach ci_status — it is the one term a user actually types."""
    assert "ci" in tool_index._tokens("ci_status")


def test_sticky_selection_is_stable_while_the_subject_is():
    """Tool definitions sit in the cached prefix, so a toolset that changes every
    turn trades a fixed cost for a recurring one. That argument is real and is why
    stickiness exists — but it justified a set that ONLY grew, and the measurement
    settled it: across six ordinary turns the set went 23 -> 26 -> 31 -> 36 -> 37
    -> 44 of 58, then latched to everything for the rest of the conversation. The
    stable prefix was bought at ~5,950 tokens a turn, forever, plus fifty-eight
    schemas for the model to choose wrongly among.

    The invariant that replaced it keeps the caching benefit where it matters:
    asking the same thing twice returns the identical toolset, so the prefix is
    stable exactly where a cache would have been hit. A bounded set cannot also be
    monotonic — that is the trade — but eviction is hysteretic (see STICKY_SLACK)
    so it happens roughly once every eight new tools rather than on every turn,
    which is what sitting exactly at the cap would have caused.
    """
    first = tool_index.select_sticky("conv1", "any messages from Vinish?")
    again = tool_index.select_sticky("conv1", "any messages from Vinish?")
    assert first is not None and again is not None
    assert set(first) == set(again), \
        "the same question twice produced a different tool block, so a turn that " \
        "should have hit the prompt cache missed it"


def test_a_conversation_never_grows_to_the_whole_registry():
    """The half the old invariant could not express."""
    from app import capabilities
    tool_index.forget("growth")
    sizes = []
    for q in ["messages from Vinish", "comment on ABC-123", "check the CI",
              "draft a mail", "trace a booking", "what is on my calendar"]:
        sel = tool_index.select_sticky("growth", q)
        sizes.append(len(capabilities.registry()) if sel is None else len(sel))
    ceiling = tool_index.STICKY_MAX_TOOLS + tool_index.STICKY_SLACK
    assert max(sizes) <= ceiling, f"grew to {max(sizes)} of {len(capabilities.registry())}: {sizes}"


def test_sticky_is_per_conversation():
    a = tool_index.select_sticky("convA", "any messages from Vinish?")
    b = tool_index.select_sticky("convB", "what is broken right now")
    assert a is not None and b is not None
    assert set(a) != set(b)


def test_forget_resets_a_conversation():
    tool_index.select_sticky("convC", "any messages from Vinish?")
    tool_index.forget("convC")
    assert "convC" not in tool_index._sticky


def test_selecting_nearly_everything_returns_none(monkeypatch):
    """Narrowing 32 tools to 31 saves nothing and risks dropping the one that
    mattered — hand back None and keep the simple path."""
    reg = capabilities.registry()
    monkeypatch.setattr(tool_index, "rank", lambda q: [(n, 1.0) for n in reg])
    assert tool_index.select("anything", k=len(reg)) is None


def test_a_wandering_conversation_ends_up_unnarrowed(monkeypatch):
    reg = capabilities.registry()
    monkeypatch.setattr(tool_index, "select", lambda q, k=8: list(reg)[:k])
    for i in range(0, len(reg), 8):
        monkeypatch.setattr(tool_index, "select",
                            lambda q, k=8, i=i: list(reg)[i:i + 8])
        out = tool_index.select_sticky("convE", "x")
    assert out is None or len(out) >= 8


# --- the embedding ranker ------------------------------------------------------
# LM Studio is not running on this machine, so the lexical path is what actually
# serves today. These fake the embedder so the preferred path is still proven —
# otherwise it would ship untested and only fail the day LM Studio comes up.

def _fake_embed(monkeypatch, tmp_path):
    """Deterministic 'embeddings': a bag-of-words vector over a fixed vocabulary."""
    vocab = sorted({t for c in capabilities.registry().values()
                    for t in tool_index._tokens(f"{c.name} {c.summary}")})

    def embed(texts):
        out = []
        for t in texts:
            toks = tool_index._tokens(t)
            out.append([1.0 if v in toks else 0.0 for v in vocab] or [0.0])
        return out

    monkeypatch.setattr("app.memory.local_embed", embed)
    monkeypatch.setattr(tool_index, "CACHE", tmp_path / "tool_index.json")
    return embed


def test_embedding_ranker_selects_and_caches(monkeypatch, tmp_path):
    _fake_embed(monkeypatch, tmp_path)
    picked = tool_index.select("post a comment on a jira issue")
    assert picked is not None
    assert "jira_comment" in picked
    assert tool_index.CACHE.exists(), "vectors must be cached, not recomputed per turn"


def test_cache_is_reused_and_invalidated_by_description_changes(monkeypatch, tmp_path):
    calls = {"n": 0}
    base = _fake_embed(monkeypatch, tmp_path)

    def counting(texts):
        calls["n"] += 1
        return base(texts)
    monkeypatch.setattr("app.memory.local_embed", counting)

    tool_index.select("jira issue")            # cold: embeds docs + query
    cold = calls["n"]
    tool_index.select("jira issue")            # warm: query only
    assert calls["n"] - cold == 1, "a warm cache must not re-embed every description"

    stored = tool_index._load_cache(tool_index._fingerprint(tool_index._docs()))
    assert stored is not None
    assert tool_index._load_cache("a-different-fingerprint") is None


def test_embedding_failure_falls_back_to_lexical(monkeypatch, tmp_path):
    monkeypatch.setattr(tool_index, "CACHE", tmp_path / "x.json")
    monkeypatch.setattr("app.memory.local_embed", lambda texts: None)
    picked = tool_index.select("any messages from Vinish?")
    assert picked is not None and "teams_activity" in picked


def test_a_corrupt_cache_is_ignored(monkeypatch, tmp_path):
    cache = tmp_path / "tool_index.json"
    cache.write_text("{ not json")
    monkeypatch.setattr(tool_index, "CACHE", cache)
    _fake_embed(monkeypatch, tmp_path)
    monkeypatch.setattr(tool_index, "CACHE", cache)
    assert tool_index.select("jira issue") is not None
