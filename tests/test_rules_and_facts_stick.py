"""Round 5, PR A: what he told Asta survives a restart and reaches every path.

25 Nov, his words: "creation date I set long back, now if I ask it's not sure,
plus standing rules and other rules are getting removed". Nothing was deleted:
the standing section was cut at the cap (the newest rules first), it never went
to code legs at all, and the identity fact sat past the slice of the index a
prompt gets."""
from __future__ import annotations

import pytest

from app import guardrails, memory


@pytest.fixture
def standing(tmp_path, monkeypatch):
    old = "".join(f"- 2026-09-{d:02d}: Old rule number {d} with enough words to take room.\n"
                  for d in range(1, 31))
    p = tmp_path / "guardrails.md"
    p.write_text("## Coding\n- Small functions.\n\n## Standing instructions\n" + old +
                 "- 2026-10-05: Dont include asta name in feature branch creation\n"
                 "- 2026-10-08: Dont include asta name in feature branch creation\n"
                 "- 2026-11-25: Always validate the data we consume from upstream\n")
    monkeypatch.setenv("ASTA_GUARDRAILS", str(p))
    guardrails._cache.clear()
    yield p
    guardrails._cache.clear()


def test_the_newest_standing_rule_is_never_cut(standing):
    assert len(guardrails.sections()["standing instructions"]) > guardrails.SECTION_MAX
    assert "validate the data we consume" in guardrails.block("chat")
    assert "cut here" not in guardrails.section("Standing instructions")


def test_standing_rules_reach_code_legs(standing):
    code = guardrails.block("code")
    assert "Dont include asta name in feature branch" in code
    assert "Small functions" in code


def test_a_rule_told_twice_is_sent_once(standing):
    body = guardrails.section("Standing instructions")
    assert body.count("asta name in feature branch") == 1
    assert "2026-10-08: Dont include" in body          # the newest dating is kept


def test_telling_a_rule_again_does_not_add_a_line(standing):
    before = standing.read_text()
    guardrails.append_standing("2026-11-20: Dont include asta name in feature branch creation")
    assert standing.read_text() == before
    guardrails.append_standing("2026-11-26: A new rule")
    assert "A new rule" in standing.read_text()


def test_standing_over_the_cap_is_not_a_health_problem(standing):
    assert "standing" not in guardrails.problems().get("guardrails", "").lower()


# --- facts ------------------------------------------------------------------------

def _fill_index(n: int) -> None:
    for i in range(n):
        memory.remember(f"Filler note number {i} about some topic", f"Filler fact {i}.", "fact")


def test_an_identity_fact_is_known_on_every_turn(monkeypatch):
    memory.remember("Asta's stated creation date", "Arun said Asta was created on January 27th 2026.")
    monkeypatch.setattr(memory, "local_embed", lambda texts: None)   # embedder down
    block = memory.turn_recall_block("hey, when were you born?")
    assert "January 27th 2026" in block


def test_identity_kind_is_pinned_whatever_the_title(monkeypatch):
    memory.remember("Default workspace", "His default workspace is booking.", "identity")
    assert any("default workspace is booking" in p["fact"] for p in memory.pinned())


def test_ordinary_facts_are_not_pinned():
    memory.remember("Kafka topic naming", "Topics end in the version.", "fact")
    assert memory.pinned() == []
    assert memory.pinned_block() == ""


def test_the_index_keeps_pinned_and_newest_entries_when_it_is_long():
    memory.remember("Asta's stated creation date", "Created on January 27th 2026.")
    _fill_index(120)
    memory.remember("The newest note", "Learned today.", "fact")
    text = memory.index_text()
    assert len(text) <= memory.INDEX_MAX_CHARS + 120
    assert "Asta's stated creation date" in text
    assert "The newest note" in text
    assert "older entries not shown" in text
    assert "Filler note number 0 " not in text            # the oldest gives way


def test_a_short_index_is_returned_as_is():
    memory.remember("One note", "Just one.", "fact")
    assert memory.index_text() == memory.INDEX_FILE.read_text()


def test_a_superseded_identity_fact_is_not_pinned():
    old = memory.remember("Asta's stated creation date", "Created on 1 Jan 2026.")
    memory.supersede(old, "Asta's stated creation date", "Created on January 27th 2026.", "corrected")
    facts = [p["fact"] for p in memory.pinned()]
    assert any("January 27th" in f for f in facts)
    assert not any("1 Jan 2026" in f for f in facts)
