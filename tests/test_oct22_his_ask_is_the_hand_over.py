"""22 Oct, task #398: an investigation he asked for stopped to ask him to confirm.

His ask reached the task as a brief another brain wrote. Nothing in it said he had
asked, so his 9 Sep standing rule — "do not investigate incidents unless I hand
one over" — read it as unasked. A task started from his own turn is the hand-over,
and the brief has to say so.
"""
import pytest

from app import agent as agent_mod, capabilities


@pytest.fixture
def spawned(monkeypatch):
    from app import relevance, tasks
    monkeypatch.setattr(relevance, "guard_spawn", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "refinable_match", lambda *a, **k: None)
    seen = {}

    def _spawn(title, prompt, *a, **k):
        seen["prompt"] = prompt
        return {"id": 398}

    monkeypatch.setattr(tasks, "spawn", _spawn)
    yield seen
    capabilities.TURN_TEXT.set("")


def test_a_task_from_his_turn_says_he_handed_it_over(spawned):
    capabilities.TURN_TEXT.set("come into conclusions with exact 100 percent accuracy "
                               "and update to Vinish")
    agent_mod.delegate_task("Pin exact run that caused equipment-id mismatch",
                            "Get the TO version history for the booking.")
    assert spawned["prompt"].endswith(agent_mod.HANDED_OVER)


def test_a_task_nobody_asked_for_this_turn_carries_no_hand_over(spawned, monkeypatch):
    """Without his words there is no hand-over to claim — the rule stays in force."""
    capabilities.TURN_TEXT.set("")
    monkeypatch.setattr(capabilities, "said_this_turn", lambda: "")
    agent_mod.delegate_task("Look at an incident mail", "Investigate INC123.")
    assert agent_mod.HANDED_OVER not in spawned["prompt"]
