"""The enrichment contract is EXTRACTED from the skill, never restated.

Two failures, a few minutes apart, both about the same file:

1. I wrote the capture rules from memory into a task prompt and silently dropped
   the front-matter half — `sources:` / `entities:` / `scenarios:` patched into
   the .md AND mirrored into repos/<repo>/_index.json. Without those, in the
   skill's own words, a captured fact is "real, correct, sourced, and
   unreachable", which is the exact rot the pass exists to undo.

2. Fixed by having the run read SKILL.md — and SKILL.md is ~6.9k tokens, most of
   it bootstrap material for a NEW repo. Re-reading all of it on every drift pass
   is ~6k tokens of waste per run.

So: the full skill provisions a new repo; this ~0.9k extract is what a
re-enrichment reads. Generated, so it cannot drift from its source — which is
the whole point, since drift is what caused (1).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path("skills/workspace-context/SKILL.md")
BRIEF = Path("skills/workspace-context/ENRICH.md")


def _build() -> str:
    sys.path.insert(0, str(BRIEF.parent))
    import build_enrich
    return build_enrich.build()


def test_the_brief_is_what_the_skill_currently_says():
    """Regenerate and compare. If this fails, SKILL.md moved and the brief did
    not: run `python skills/workspace-context/build_enrich.py`."""
    assert BRIEF.read_text() == _build(), (
        "ENRICH.md is stale — regenerate it from SKILL.md")


def test_it_is_dramatically_cheaper_than_the_whole_skill():
    """The reason it exists. If the gap closes, the extraction has stopped
    extracting and is pulling the whole file along."""
    assert len(BRIEF.read_text()) < len(SKILL.read_text()) / 4


def test_it_carries_the_half_that_was_lost():
    """The front-matter rules — the ones a paraphrase dropped, and the ones that
    decide whether a captured fact can ever be found again."""
    body = BRIEF.read_text()
    for must in ("sources:", "entities:", "scenarios:", "_index.json",
                 "resolve-task.js", "150 lines"):
        assert must in body, f"the brief lost {must!r}"


def test_it_carries_the_rule_against_bloat():
    """"dont bloat with unwanted info" — a useless line is a permanent tax on
    every future task, not a neutral addition."""
    assert "Adding nothing is better than adding noise" in BRIEF.read_text()


def test_it_is_marked_generated():
    """A hand-edit here would reintroduce exactly the drift this prevents."""
    head = BRIEF.read_text()[:300]
    assert "GENERATED" in head and "do not edit by hand" in head


def test_the_generator_is_idempotent():
    before = BRIEF.read_text()
    subprocess.run([sys.executable, "skills/workspace-context/build_enrich.py"],
                   capture_output=True, check=True)
    assert BRIEF.read_text() == before
