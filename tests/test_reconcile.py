"""'Delivery not confirmed' is settled from the stored chat, never by resending."""
import asyncio
import json
import time

from app import reconcile, store, task_inform
from tests.test_task_inform import code_task, teams  # noqa: F401

TEXT = "Hi Vinish, PR is up for Retry fix — booking PR 1409"


def receipt(key, to="Vinish Kumar", text=TEXT, at=None, state="uncertain"):
    at = time.time() - 900 if at is None else at
    store.kv_set(key, json.dumps({"state": state, "intent": {"to": to, "what": text}, "at": at}))
    return at


def history(chat, *rows):
    store.save_teams_messages([{"key": f"{chat}:{i}", "chat": chat, "sender": s, "text": t,
                                "sent_at": at} for i, (s, t, at) in enumerate(rows)])


def state(key):
    return json.loads(store.kv_get(key))


def test_found_in_the_chat_settles_as_sent_and_says_nothing():
    at = receipt("answer_delivery:a")
    history("Vinish Kumar", ("Vinish Kumar", "earlier", at - 3600), ("me", TEXT, at + 5))
    assert asyncio.run(reconcile.sweep()) == []
    assert state("answer_delivery:a")["state"] == "sent"


def test_absent_from_a_chat_read_after_the_send_is_said_once_and_not_resent():
    at = receipt("answer_delivery:b")
    history("Vinish Kumar", ("Vinish Kumar", "earlier", at - 3600), ("Vinish Kumar", "hello?", at + 400))
    told = asyncio.run(reconcile.sweep())
    assert len(told) == 1 and "did not arrive" in told[0] and "Nothing was resent" in told[0]
    assert state("answer_delivery:b")["state"] == "not_delivered"
    assert asyncio.run(reconcile.sweep()) == []


def test_the_same_words_from_them_are_not_his_delivery():
    at = receipt("answer_delivery:c")
    history("Vinish Kumar", ("Vinish Kumar", "earlier", at - 3600), ("Vinish Kumar", TEXT, at + 5))
    assert "did not arrive" in asyncio.run(reconcile.sweep())[0]


def test_unread_history_stays_unknown_then_is_said_once_after_a_day():
    receipt("answer_delivery:d")
    assert asyncio.run(reconcile.sweep()) == [] and state("answer_delivery:d")["state"] == "uncertain"
    receipt("answer_delivery:e", at=time.time() - reconcile.GIVE_UP_SECONDS - 60)
    told = asyncio.run(reconcile.sweep())
    assert len(told) == 1 and "Still can't tell" in told[0]
    assert asyncio.run(reconcile.sweep()) == []


def test_a_crash_mid_send_is_checked_and_a_send_in_progress_is_not():
    at = receipt("answer_delivery:f", state="sending")
    receipt("answer_delivery:g", state="sending", at=time.time() - 30)
    history("Vinish Kumar", ("Vinish Kumar", "earlier", at - 3600), ("me", TEXT, at + 5))
    asyncio.run(reconcile.sweep())
    assert state("answer_delivery:f")["state"] == "sent"
    assert state("answer_delivery:g")["state"] == "sending"


def test_a_task_update_receipt_keeps_the_words_and_is_settled(teams):
    sent, st, _ = teams
    tid = code_task("raise PR and inform Vinish")
    st["fail"] = RuntimeError("Teams page closed")
    asyncio.run(task_inform.reached(tid, "pr"))
    key = f"task_inform_receipt:{tid}:pr"
    rec = json.loads(store.kv_get(key))
    assert rec["state"] == "uncertain" and rec["text"].startswith("Hi Vinish") and rec["at"]
    history("Vinish", ("Vinish", "earlier", rec["at"] - 3600), ("me", rec["text"], rec["at"] + 3))
    asyncio.run(reconcile.sweep(now=rec["at"] + 1000))
    assert json.loads(store.kv_get(key))["state"] == "sent" and sent == []
