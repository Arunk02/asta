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


class _Child:
    """A fake `python -m app.workworld run` — writes its summary like the real one."""

    def __init__(self, cmd, env, summary=None, hang=False):
        self.cmd, self.env, self.returncode = cmd, env, 0
        self._summary, self._hang = summary, hang

    async def communicate(self):
        import asyncio
        import json
        from pathlib import Path
        if self._hang:
            await asyncio.sleep(3600)
        if self._summary is not None:
            Path(self.cmd[self.cmd.index("--json-out") + 1]).write_text(json.dumps(self._summary))
        else:
            self.returncode = 2
        return b"scenario died", None

    def kill(self):
        pass


def _spawned(monkeypatch, **kw):
    import asyncio
    seen = {}

    async def spawn(*cmd, env=None, **_):
        seen["cmd"], seen["env"] = list(cmd), env or {}
        seen["installed_meanwhile"] = W.installed()
        seen["child"] = _Child(list(cmd), env or {}, **kw)
        return seen["child"]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return seen


@pytest.mark.asyncio
async def test_measuring_runs_the_bench_in_its_own_process(monkeypatch):
    """14 Oct: a nightly pass started at 01:26 never gave the doors back, and for
    ten and a half hours his WhatsApp was answered "(nothing scripted)"."""
    from app import evolve, notify
    real = notify.notify
    seen = _spawned(monkeypatch, summary={"pass_rate": 1.0, "day": {}})
    got = await evolve.measure()
    assert got == {"pass_rate": 1.0, "day": {}}
    assert seen["cmd"][1:4] == ["-m", "app.workworld", "run"]
    assert "--no-save" in seen["cmd"]
    assert not seen["installed_meanwhile"] and not W.installed()
    assert notify.notify is real


@pytest.mark.asyncio
async def test_a_bench_that_dies_leaves_this_process_untouched(monkeypatch):
    from app import evolve, notify
    real = notify.notify
    _spawned(monkeypatch, summary=None)
    with pytest.raises(RuntimeError, match="no summary"):
        await evolve.measure()
    assert notify.notify is real and not W.installed()


@pytest.mark.asyncio
async def test_a_bench_that_hangs_is_killed(monkeypatch):
    from app import evolve
    monkeypatch.setattr(evolve, "MEASURE_TIMEOUT", 0.05)
    _spawned(monkeypatch, hang=True)
    with pytest.raises(RuntimeError, match="did not finish"):
        await evolve.measure()


@pytest.mark.asyncio
async def test_the_guard_takes_a_leaked_world_back_out_and_says_so(monkeypatch):
    import asyncio
    from app import evolve, notify
    told = []
    _world()                                    # leaked: nobody uninstalls it

    async def tell(text, *a, **k):
        told.append(text)

    async def ticks(_):
        ticks.n += 1
        if ticks.n > 3:
            raise asyncio.CancelledError
    ticks.n = 0
    monkeypatch.setattr(asyncio, "sleep", ticks)
    real_restore = W.restore_all

    def restore_then_listen():
        # The guard puts Asta's real notify back, then speaks through it; record
        # what it says instead of sending it anywhere.
        left = real_restore()
        monkeypatch.setattr(notify, "notify", tell)
        return left

    monkeypatch.setattr(W, "restore_all", restore_then_listen)
    try:
        with pytest.raises(asyncio.CancelledError):
            await evolve.guard_loop(interval=0)
    finally:
        if W.installed():
            real_restore()
    assert not W.installed()
    assert told and "test world" in told[0]
