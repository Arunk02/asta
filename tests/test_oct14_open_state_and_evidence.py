"""14 Oct audit, P2 and P3.

P2: "yes post it" re-staged a second review because the brain could not see one
waiting; a later turn said "#306 failed — nothing real to post" while #307 had
finished that review. Every turn now carries what is open.
P3: chat questions about one booking reached the brain with no evidence; 0 of 42
investigations used the debug kit. Where the id is logged is fetched first.
"""

import asyncio
import time

import pytest

from app import booking_case, copilot_cli, loop, offers, store


def test_the_offer_waiting_for_his_yes_is_in_every_turn():
    offers.drop_all()
    offers.staged_write("pr_review", {"pr": "1483", "action": "request_changes", "body": "coupling"},
                        "🔎 Request changes on PR #1483", "Booking.v9.avsc imports ServicePlan…",
                        "Request changes?", kind="pr_write")
    state = copilot_cli.conversation_state("")
    assert "WAITING FOR HIS YES" in state and "Request changes on PR #1483" in state
    assert "never stage a second copy" in state


def test_a_waiting_draft_is_in_the_turn():
    cid = store.create_conversation(model="x", workspace=None)["id"]
    loop.stage(cid, {"type": "send", "to": "Komal Jayswal", "what": "PR 1483 looks good", "channel": "teams"})
    assert "to Komal Jayswal" in copilot_cli.conversation_state(cid)


def test_recent_tasks_show_how_they_ended():
    failed = store.create_task("Review Vinish Kumar's PR #1483", "analysis", "p", None)
    store.update_task(failed["id"], status="failed", error="claude one-shot timed out")
    done = store.create_task("Review Vinish Kumar's PR #1483 (retry)", "analysis", "p", None)
    store.update_task(done["id"], status="done",
                      result="ANALYSIS:\n1. Staged 4 inline comments on PR 1483\nREPLY:\nx")
    state = copilot_cli.conversation_state("")
    assert f"task #{failed['id']} failed" in state and "timed out" in state
    assert f"task #{done['id']} done" in state and "Staged 4 inline comments" in state
    assert "superseded by the done one" in state


def test_nothing_open_means_nothing_added(monkeypatch):
    offers.drop_all()
    monkeypatch.setattr(store, "list_tasks", lambda limit=40: [])
    assert copilot_cli.conversation_state("") == ""


def test_the_state_rides_in_turn_context():
    offers.drop_all()
    offers.staged_write("pr_review", {"pr": "93", "action": "comment", "body": "x"},
                        "🔎 Comment on PR #93", "x", "Comment?", kind="pr_write")
    assert "Comment on PR #93" in copilot_cli.turn_context("yes post it", "")


def test_a_ring_offer_is_gone_after_two_minutes():
    offers.drop_all()
    o = offers.offer("call", "📞 Vinish Kumar is calling", "1:1", "Answer it?")
    assert not o.expired()
    assert o.expired(now=o.created + offers.CALL_OFFER_SECONDS + 1)
    other = offers.offer("analyse", "CI failure", "x", "Analyse?")
    assert not other.expired(now=other.created + offers.CALL_OFFER_SECONDS + 1)


# --- P3: evidence before the brain -------------------------------------------------------

@pytest.fixture
def counted(monkeypatch):
    from app import agent
    calls = []

    async def log_counts(term="", env="", by="app", hours=24, **k):
        calls.append((term, env))
        return f"Lines with “{term}” by app: - {env}: telikos-billing-service 41"

    async def nothing(*a, **k):
        return ""

    monkeypatch.setattr(agent, "log_counts", log_counts)
    monkeypatch.setattr(agent, "temporal_workflows", nothing)
    monkeypatch.setattr(agent, "log_lines", nothing)
    return calls


def test_a_booking_question_gets_its_evidence_first(counted):
    out = asyncio.run(booking_case.turn_evidence("why is booking H7Y5BWNBNX9 not consumed in prod"))
    assert counted == [("H7Y5BWNBNX9", "prod")]
    assert "Evidence fetched in code" in out and "telikos-billing-service 41" in out


def test_no_environment_named_means_every_environment(counted):
    asyncio.run(booking_case.turn_evidence("check booking H7Y5BWNBNX9 status"))
    assert counted == [("H7Y5BWNBNX9", "all")]


@pytest.mark.parametrize("text", ["hi how are you", "send the PR to Vinish", "H7Y5BWNBNX9"])
def test_no_id_or_no_checking_means_no_evidence(counted, text):
    assert asyncio.run(booking_case.turn_evidence(text)) == ""
    assert counted == []


def test_slow_logs_never_hold_the_turn(monkeypatch):
    from app import agent

    async def slow(*a, **k):
        await asyncio.sleep(5)
        return "late"

    monkeypatch.setattr(agent, "log_counts", slow)
    monkeypatch.setattr(booking_case, "TURN_EVIDENCE_SECONDS", 0.05)
    t = time.monotonic()
    assert asyncio.run(booking_case.turn_evidence("why booking H7Y5BWNBNX9 stuck")) == ""
    assert time.monotonic() - t < 1


def test_both_brains_get_the_evidence():
    import inspect
    from app import claude_cli
    assert "turn_evidence" in inspect.getsource(claude_cli.run_turn)
    assert "turn_evidence" in inspect.getsource(copilot_cli.run_turn)


# --- "do not push or message anyone" is not a request to message anyone -------------------

ARUDDHA_ASK = ("On booking PR 1482, Aruddha reviewed it and said: looks good, just add a test. Add unit "
               "tests on that PR's branch covering all the scenarios. Run the tests. Do not push or "
               "message anyone — show me the plan and the result.")


@pytest.mark.parametrize("text,talk", [
    ("Do not push or message anyone", False),
    ("dont push, commit or ping Vinish", False),
    ("never call or message him", False),
    ("message Vinish about it", True),
    ("Do not push. Message Vinish when done", True),
])
def test_a_negation_covers_the_list_of_verbs_after_it(text, talk):
    from app import consent
    assert consent.asked_to_talk(text) is talk


def test_a_code_ask_that_forbids_messaging_is_not_blocked_as_talk():
    from app import consent
    assert consent.substitution(ARUDDHA_ASK, "code") == ""
