"""One note per fact: an exact repeat is not written again, and a note close in
meaning to one on file is put to the brain before it is saved.

17 Nov: two corrections were on file 12 and 11 times; "topic refresh" lived in
two different repos depending on which note recall found first."""
from __future__ import annotations

from app import agent, memory


def test_the_same_note_is_not_written_twice():
    a = memory.remember("Correction: why do you always stop?", "Keep going until done.", "gotcha")
    b = memory.remember("Correction: why do you always stop?", "Keep  going until done.", "gotcha")
    assert a == b
    assert sum(1 for m in memory.list_memories()
               if m["title"] == "Correction: why do you always stop?") == 1


def test_a_close_note_is_put_to_the_brain_first(monkeypatch):
    memory.remember("Topic refresh lives in repo A", "Topic refresh work is in repo A.")
    monkeypatch.setattr(memory, "local_embed", lambda texts: [[1.0, 0.0]] + [[0.95, 0.05]] * (len(texts) - 1))
    out = agent.remember("Topic refresh lives in repo B", "Topic refresh work is in repo B.")
    assert out.startswith("Not saved yet") and "correct_memory" in out and "distinct=true" in out
    assert not any(m["title"] == "Topic refresh lives in repo B" for m in memory.list_memories())
    out = agent.remember("Topic refresh lives in repo B", "Topic refresh work is in repo B.",
                         distinct=True)
    assert out.startswith("Remembered")


def test_unrelated_notes_and_no_embedder_save_as_before(monkeypatch):
    memory.remember("Grafana needs the VPN", "The proxy only answers on the office VPN.")
    monkeypatch.setattr(memory, "local_embed", lambda texts: [[1.0, 0.0]] + [[0.0, 1.0]] * (len(texts) - 1))
    assert agent.remember("Deploys are slow on Mondays", "Pipeline queue.").startswith("Remembered")
    monkeypatch.setattr(memory, "local_embed", lambda texts: None)
    assert agent.remember("Standups at 9:15", "Daily.").startswith("Remembered")
