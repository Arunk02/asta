"""A chat turn is handed his curated memory unasked, never old chat digests (16 Nov)."""

import pytest

from app import copilot_cli, memory, project_knowledge


def _hits(*rows):
    return [{"path": f"{k}s/{n}.md", "title": n, "snippet": n, "mtype": k, "date": "2026-11-01"}
            for k, n in rows]


@pytest.fixture
def scored(monkeypatch):
    rows = _hits(("episode", "chat-about-reminders"), ("fact", "pr-link-rule"),
                 ("gotcha", "tms-country-first"), ("episode", "live-test-one-line"))
    sims = {"chat-about-reminders": 0.72, "pr-link-rule": 0.69, "tms-country-first": 0.55,
            "live-test-one-line": 0.70}
    monkeypatch.setattr(memory.store, "memory_search", lambda q, n, workspace="": rows)

    def embed(texts):
        return [[1.0]] + [[sims[t.split()[0]]] for t in texts[1:]]
    monkeypatch.setattr(memory, "local_embed", embed)
    monkeypatch.setattr(memory, "_cosine", lambda a, b: b[0])
    return rows


def test_a_turn_gets_curated_notes_above_the_bar_and_no_chat_digests(scored):
    hits = memory.recall("remind me to chase the PR", k=3, kinds=memory.TURN_KINDS,
                         floor=memory.TURN_RECALL_FLOOR)
    assert [h["title"] for h in hits] == ["pr-link-rule"]


def test_search_memory_still_reaches_past_chats(scored):
    titles = [h["title"] for h in memory.recall("remind me to chase the PR")]
    assert "chat-about-reminders" in titles


def test_without_an_embedder_a_turn_gets_nothing_unasked(monkeypatch, scored):
    monkeypatch.setattr(memory, "local_embed", lambda texts: None)
    assert memory.turn_recall_block("remind me to chase the PR") == ""
    assert memory.recall("remind me to chase the PR"), "an explicit search still answers"


@pytest.mark.parametrize("text,lookup", [
    ("What reminders do I have set right now?", False),
    ("status of my open PRs?", False),
    ("where does customs feedback come from?", True),
    ("how many TOs for a normal booking?", True),
    ("explain the activity plan flow", True),
])
def test_a_turn_reads_his_documents_only_for_a_project_question(text, lookup):
    assert project_knowledge.is_knowledge_question(text) is lookup


def test_the_turn_uses_the_shared_knowledge_test(monkeypatch):
    calls = []
    monkeypatch.setattr(project_knowledge, "lookup", lambda q, **k: calls.append(q) or "PASSAGE")
    copilot_cli.turn_context("What reminders do I have set right now?", "")
    assert calls == []
    out = copilot_cli.turn_context("where does customs feedback come from?", "")
    assert calls and "PASSAGE" in out
