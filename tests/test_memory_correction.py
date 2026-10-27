"""A wrong fact is corrected, not contradicted; history restores byte for byte;
deletion happens only on his word and says exactly what it removed."""
import hashlib

import pytest

from app import agent, capabilities, memory, store
from tests.test_memory_continuity import mem  # noqa: F401


def _said(text):
    return capabilities.TURN_TEXT.set(text)


def test_a_correction_supersedes_and_leaves_recall(mem):
    old = memory.remember("Booking retry timeout", "Booking retry timeout is 30s.", workspace="booking")
    line = agent.correct_memory(old, "Booking retry timeout", "Booking retry timeout is 45s since 3.1.6.",
                                why="changed in release 3.1.6")
    assert "superseded" in line
    hits = memory.recall("booking retry timeout", workspace="booking")
    assert [h["snippet"] for h in hits if "30s" in h["snippet"]] == []
    assert any("45s" in h["snippet"] for h in hits)
    old_text = memory.read_memory_file(old)
    assert "Booking retry timeout is 30s." in old_text, "the old body is never rewritten"
    meta = memory._parse_frontmatter(old_text)
    assert meta["superseded_by"] and meta["why"] == "changed in release 3.1.6"
    new_meta = memory._parse_frontmatter(memory.read_memory_file(meta["superseded_by"]))
    assert new_meta["supersedes"] == old and new_meta["workspace"] == "booking"
    assert f"]({old})" not in memory.INDEX_FILE.read_text()


def test_a_fact_is_corrected_once_and_episodes_are_not_facts(mem):
    old = memory.remember("Env", "QA is on cluster A.")
    agent.correct_memory(old, "Env", "QA is on cluster B.")
    assert "already corrected" in agent.correct_memory(old, "Env", "QA is on cluster C.")
    (memory.EPISODES_DIR / "e.md").write_text("---\ntitle: talk\ntype: episode\n---\nx")
    assert "No stored fact" in agent.correct_memory("episodes/e.md", "t", "f")
    assert "No stored fact" in agent.correct_memory("../../etc/passwd", "t", "f")


def test_the_restore_drill_reports_then_restores_byte_for_byte(mem):
    a = memory.remember("Alpha", "Alpha uses namespace one.")
    b = memory.remember("Beta", "Beta uses namespace two.")
    before = {p: (memory.MEMORY_DIR / p).read_bytes() for p in (a, b)}
    memory.consolidate()                                     # writes the snapshot
    (memory.MEMORY_DIR / a).unlink()
    (memory.MEMORY_DIR / b).write_text("tampered")
    drill = memory.restore()
    assert drill["missing"] == [a] and drill["changed"] == [b] and drill["restored"] == []
    assert (memory.MEMORY_DIR / b).read_text() == "tampered", "a drill changes nothing"
    done = memory.restore(apply=True)
    assert sorted(done["restored"]) == sorted([a, b])
    for p, raw in before.items():
        assert (memory.MEMORY_DIR / p).read_bytes() == raw
    tampered = hashlib.sha256(b"tampered").hexdigest()
    assert (memory.MEMORY_DIR / ".history" / f"{tampered}.md").exists(), "the overwritten version was kept"
    assert memory.recall("alpha namespace")


def test_a_corrupt_history_copy_is_reported_never_restored(mem):
    a = memory.remember("Alpha", "Alpha uses namespace one.")
    memory.consolidate()
    digest = hashlib.sha256((memory.MEMORY_DIR / a).read_bytes()).hexdigest()
    (memory.MEMORY_DIR / ".history" / f"{digest}.md").write_text("bit rot")
    (memory.MEMORY_DIR / a).unlink()
    assert memory.restore(apply=True)["corrupt"] == [a]
    assert not (memory.MEMORY_DIR / a).exists()


@pytest.mark.parametrize("said", ["what do you know about alpha?", "alpha is wrong"])
def test_nothing_is_deleted_without_his_words(mem, said):
    a = memory.remember("Alpha", "Alpha uses namespace one.")
    token = _said(said)
    try:
        assert "Not deleted" in agent.forget_memory(a)
    finally:
        capabilities.TURN_TEXT.reset(token)
    assert (memory.MEMORY_DIR / a).exists()


def test_forget_removes_from_memory_search_and_index_but_stays_restorable(mem):
    a = memory.remember("Alpha", "Alpha uses namespace one.")
    memory.consolidate()
    token = _said("forget the alpha namespace note")
    try:
        assert "can be restored" in agent.forget_memory(a)
    finally:
        capabilities.TURN_TEXT.reset(token)
    assert not (memory.MEMORY_DIR / a).exists()
    assert not store.memory_search("alpha namespace")
    assert f"]({a})" not in memory.INDEX_FILE.read_text()
    assert memory.restore(paths=[a])["missing"] == [a]
    assert memory.restore(paths=[a], apply=True)["restored"] == [a]


def test_purge_needs_his_explicit_word_and_removes_every_copy(mem):
    a = memory.remember("Secret", "The token is xyz.")
    memory.consolidate()
    token = _said("forget the secret note")
    try:
        assert "Not purged" in agent.forget_memory(a, purge=True)
    finally:
        capabilities.TURN_TEXT.reset(token)
    token = _said("delete the secret note permanently")
    try:
        assert "purged" in agent.forget_memory(a, purge=True)
    finally:
        capabilities.TURN_TEXT.reset(token)
    history = memory.MEMORY_DIR / ".history"
    assert not any("xyz" in p.read_text() for p in history.glob("*.md"))
    assert memory.restore(paths=[a], apply=True)["purged"] == [a]
    assert not (memory.MEMORY_DIR / a).exists()
