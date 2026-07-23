"""ask_user: one question, answered from any channel, without stopping the work."""

from __future__ import annotations

import asyncio

import pytest

from app import asking, store


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()
    asking._waiters.clear()
    monkeypatch.setattr("app.notify.notify",
                        lambda *a, **k: asyncio.sleep(0))


def test_answer_unblocks_the_caller():
    async def scenario():
        task = asyncio.create_task(asking.ask("which repo?", "test"))
        await asyncio.sleep(0.05)
        qid = store.open_questions()[0]["id"]
        assert asking.answer(qid, "the booking one")
        return await task
    assert asyncio.run(scenario()) == "the booking one"


def test_timeout_degrades_instead_of_failing():
    """A clarifying question that goes unanswered must not fail the work it
    was clarifying."""
    out = asyncio.run(asking.ask("still there?", "test", timeout=0.05))
    assert out == asking.NO_ANSWER
    assert store.open_questions() == []


def test_empty_question_is_not_asked():
    assert asyncio.run(asking.ask("   ")) == asking.NO_ANSWER
    assert store.open_questions() == []


def test_answering_twice_is_refused():
    async def scenario():
        task = asyncio.create_task(asking.ask("which one?", "test"))
        await asyncio.sleep(0.05)
        qid = store.open_questions()[0]["id"]
        first = asking.answer(qid, "A")
        await task
        return first, asking.answer(qid, "B")
    first, second = asyncio.run(scenario())
    assert first and not second


def test_answering_an_unknown_question_is_false():
    assert not asking.answer(999, "hello")


def test_bare_reply_routes_only_when_one_question_is_open():
    store.create_question("which repo?", "test")
    assert asking.pending_for_reply() is not None
    store.create_question("and which branch?", "test")
    # Two open: guessing would put the answer on the wrong one, and on a phone
    # channel that is invisible until it has already gone wrong.
    assert asking.pending_for_reply() is None


def test_a_stale_question_stops_swallowing_messages(monkeypatch):
    q = store.create_question("old thing?", "test")
    store.close_question(q["id"], "", status="open")   # keep it open, age it
    with store._connect() as conn:
        conn.execute("UPDATE questions SET created_at=? , status='open' WHERE id=?",
                     (0.0, q["id"]))
    assert asking.pending_for_reply() is None


def test_expire_stale_clears_orphans():
    store.create_question("orphan?", "test")
    assert asking.expire_stale() == 1
    assert store.open_questions() == []


def test_outcomes_are_recorded():
    async def scenario():
        task = asyncio.create_task(asking.ask("q?", "test"))
        await asyncio.sleep(0.05)
        asking.answer(store.open_questions()[0]["id"], "a")
        await task
    asyncio.run(scenario())
    asyncio.run(asking.ask("q2?", "test", timeout=0.05))
    kinds = {(r["kind"], r["outcome"]) for r in store.recent_outcomes()}
    assert ("ask", "answered") in kinds
    assert ("ask", "timeout") in kinds
