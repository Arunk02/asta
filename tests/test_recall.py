"""Memory recall must help or stay silent — never misdirect.

From a real WhatsApp exchange, 31 July. Arun asked, about a 300-second Teams-send
timeout on his screen:

    Arun:  What is this error
    Asta:  That recalled memory is an old one — from 2026-07-21, about an IAM
           Token API error (ERR_GW_001) in the email service, traced to the
           shared Document Storage / Finance Chassis client_id-secret…

He asked why a Teams send broke and got a ten-day-old note about document storage.
The recall layer OR-matched every memory containing the word "error", returned the
top FTS hit, and labelled it "Relevant memories (recalled automatically)" — so the
model trusted it and answered from it.

An unrelated memory is worse than none: it doesn't merely fail to help, it steers
the answer wrong. These tests pin that a generic question recalls nothing, a real
one still recalls, and what is surfaced is framed as "ignore if it doesn't fit".
"""

from __future__ import annotations

import pathlib

import pytest

from app import memory, store


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A tiny memory index around the exact memories from that day."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "recall.db", raising=False)
    store.init()
    store.memory_reindex([
        {"path": "episodes/err-gw-001.md", "title": "IAM Token API error ERR_GW_001",
         "mtype": "episode", "date": "2026-07-21",
         "body": "IAM Token API error ERR_GW_001 in the email service — traced to the shared "
                 "Document Storage / Finance Chassis client_id-secret pointing at api-cdt.maersk.com"},
        {"path": "facts/teams-ping.md", "title": "Ping people personally", "mtype": "fact",
         "date": "2026-07-27", "body": "ping X means X's 1:1 Teams DM, never a group"},
        {"path": "episodes/vinish-pr.md", "title": "Vinish PR review", "mtype": "episode",
         "date": "2026-07-31",
         "body": "Reviewed PR 1333 telikos-booking-service rental chassis references, "
                 "flagged duplicate validator code"},
    ])
    return store


def _titles(hits):
    return {h["title"] for h in hits}


# --- FTS-only path (LM Studio down, which is the state that bit him) ----------

def test_a_generic_question_recalls_nothing(seeded, monkeypatch):
    """THE bug. "What is this error" carries no topic — every word is generic — so
    it must pull nothing, not the highest-bm25 memory that happens to say 'error'."""
    monkeypatch.setattr(memory, "local_embed", lambda texts: [])   # no embedder
    assert memory.recall("What is this error") == []


def test_the_send_question_does_not_surface_document_storage(seeded, monkeypatch):
    monkeypatch.setattr(memory, "local_embed", lambda texts: [])
    hits = memory.recall("why is it breaking while sending to vinish")
    assert "IAM Token API error ERR_GW_001" not in _titles(hits)


def test_a_real_query_still_finds_the_memory(seeded, monkeypatch):
    """The floor must not gut genuine recall — asked about that memory by its real
    terms, it still comes back."""
    monkeypatch.setattr(memory, "local_embed", lambda texts: [])
    hits = memory.recall("IAM token document storage client secret")
    assert "IAM Token API error ERR_GW_001" in _titles(hits)


def test_a_pure_meta_question_recalls_nothing(seeded, monkeypatch):
    monkeypatch.setattr(memory, "local_embed", lambda texts: [])
    assert memory.recall("what does this mean") == []


def test_all_stopword_query_short_circuits_at_the_search_layer(seeded):
    """store.memory_search itself returns nothing when only generic words remain,
    so nothing downstream can resurrect them."""
    assert store.memory_search("what is this error going wrong") == []
    assert store.memory_search("chassis validator") != []   # a real term still matches


# --- semantic path (LM Studio up) has a relevance floor ----------------------

def _fake_embedder(relevant_marker: str):
    """query -> [1,0]; a hit containing the marker -> [1,0] (cos 1), else [0,1] (cos 0)."""
    def embed(texts):
        out = []
        for i, t in enumerate(texts):
            if i == 0:
                out.append([1.0, 0.0])                     # the query
            else:
                out.append([1.0, 0.0] if relevant_marker in t else [0.0, 1.0])
        return out
    return embed


def test_the_floor_drops_a_semantically_unrelated_hit(seeded, monkeypatch):
    """Even if FTS returns it, a memory the embedder scores below the floor is not
    surfaced — meaning, not keyword coincidence, decides."""
    monkeypatch.setattr(memory, "local_embed", _fake_embedder("chassis"))
    monkeypatch.setattr(memory, "RECALL_FLOOR", 0.35)
    hits = memory.recall("rental chassis")
    assert _titles(hits) <= {"Vinish PR review"}          # only the on-topic one, if any
    assert "IAM Token API error ERR_GW_001" not in _titles(hits)


def test_recency_cannot_lift_a_below_floor_memory(seeded, monkeypatch):
    """The floor is checked on similarity alone, so a very recent but irrelevant
    note cannot ride its recency bonus over the bar."""
    monkeypatch.setattr(memory, "local_embed",
                        lambda texts: [[0.0, 1.0]] * len(texts))   # everything scores ~0
    assert memory.recall("anything at all today") == []


# --- framing -----------------------------------------------------------------

def test_recall_block_does_not_assert_relevance(seeded, monkeypatch):
    """"Relevant memories" is the phrasing that made the model trust a marginal
    hit. Whatever survives must be offered as ignorable."""
    monkeypatch.setattr(memory, "local_embed", lambda texts: [])
    block = memory.recall_block("IAM token document storage")
    assert block
    assert "Relevant memories" not in block
    assert "ignore" in block.lower()


def test_recall_block_is_empty_when_nothing_clears_the_bar(seeded, monkeypatch):
    """No block at all beats an empty-but-present one the model might pad an answer
    around."""
    monkeypatch.setattr(memory, "local_embed", lambda texts: [])
    assert memory.recall_block("what is this error") == ""
