"""The bench runs inside the LIVE server, and must give the doors back.

`evolve.measure` runs the whole scenario bench in the running process, on the
same module objects the daemons are using. So a world that never comes back out
is not a test problem — it is an outage with no error message: Asta keeps
reading Teams, keeps ranking it, marks each item "notified", and every push
disappears into a recorder nobody will ever read.

That happened. Twenty-two hours, three hundred messages, not one word to him.
The three guards below are what makes it recoverable rather than invisible:

  · a registry, so "is a bench holding Asta's doors right now?" is answerable;
  · `restore_all`, so the live entry point can insist on a clean process even
    when a scenario dies in a way its own `finally` never saw;
  · a live loop that stands down while the bench holds the doors, exactly as it
    already stands down during a call.
"""

from __future__ import annotations

import pytest

from app.workworld import world as W


def _world() -> W.World:
    w = W.World()
    w.install(chat_brain=W.ScriptedBrain([], w, "chat"),
              task_brain=W.ScriptedBrain([], w, "task"))
    return w


def test_a_world_in_place_is_answerable_and_a_clean_process_says_so():
    assert not W.installed(), "a previous test left the bench's doors in place"
    w = _world()
    try:
        assert W.installed()
    finally:
        w.uninstall()
    assert not W.installed()


def test_restore_all_takes_back_a_world_that_leaked():
    """The safety net: the caller's own finally never ran, and the live process
    is still holding a recorder where its notify used to be."""
    from app import notify
    real = notify.notify
    w = _world()
    assert notify.notify is not real, "the doubles did not go in"
    left = W.restore_all()                  # nobody called w.uninstall()
    assert notify.notify is real, "a leaked world was not taken back out"
    assert left, "restore_all must say what it had to clean up"
    assert not W.installed()


@pytest.mark.asyncio
async def test_the_chat_sweep_stands_down_while_the_bench_holds_the_doors():
    """A sweep during a bench run reads the bench's database and pushes into the
    bench's recorder. It is the same reason the loop stands down during a call."""
    from app import chat_watch
    w = _world()
    try:
        assert chat_watch.stand_down(), (
            "the chat loop must not sweep while the bench owns Asta's doors")
    finally:
        w.uninstall()
    assert not chat_watch.stand_down()


@pytest.mark.asyncio
async def test_measuring_gives_the_doors_back_even_when_the_bench_blows_up():
    from app import evolve, notify
    real = notify.notify

    async def explode(*a, **k):
        _world()                            # installs, and never comes back out
        raise RuntimeError("a scenario died mid-run")

    from app.workworld import runner
    was = runner.run_all
    runner.run_all = explode
    try:
        with pytest.raises(RuntimeError):
            await evolve.measure()
    finally:
        runner.run_all = was
        W.restore_all()
    assert notify.notify is real, (
        "a bench run that blew up left Asta unable to tell him anything")
