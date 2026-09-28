"""The bench's doubles must be able to stand in for the real doors.

This exists because of a silence. For twenty-two hours Asta read Teams
perfectly — three hundred messages stored, every one ranked — and told him
nothing. The chat sweep was dying on its last line:

    TypeError: World.install.<locals>.notify_fn() got an unexpected keyword
               argument 'keys'

`chat_watch.sweep` passes `keys=` so the attention ledger can tie one push to
the rows it covers. The real `notify.notify` grew that parameter; the bench's
stand-in did not. Every check was green — the doubles are only ever called by
scenarios, which do not pass `keys` — so nothing in the suite could see it.

Two tests, both generic on purpose. The first compares every double against
the door it replaces, so the next parameter somebody adds is caught here
instead of in a quiet morning. The second proves the doubles come back out
again even when a run dies before its first step.
"""

from __future__ import annotations

import inspect

import pytest

from app.workworld import world as W


def _keywords(fn) -> tuple[set[str], bool]:
    """Every name this callable accepts as a keyword, and whether it takes **kw."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):                      # a builtin, or unreadable
        return set(), True
    names = {p.name for p in params
             if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    var_kw = any(p.kind is p.VAR_KEYWORD for p in params)
    return names, var_kw


def _positional(fn) -> int:
    """How many arguments this callable accepts positionally (-1 for *args)."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return -1
    if any(p.kind is p.VAR_POSITIONAL for p in params):
        return -1
    return sum(1 for p in params
               if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))


def _installed_world():
    """A World with its doubles in place, and the pairs it swapped."""
    world = W.World()
    world.install(chat_brain=W.ScriptedBrain([], world, "chat"),
                  task_brain=W.ScriptedBrain([], world, "task"))
    # (module, name, the real thing, the double) for everything the bench replaced.
    pairs = [(obj, name, old, getattr(obj, name, None))
             for obj, name, old, had in world._patch._undo if had]
    return world, pairs


def test_every_double_accepts_every_call_the_real_door_accepts():
    """A stand-in narrower than the thing it stands in for is a live outage
    waiting for the one caller that uses the parameter it dropped."""
    world, pairs = _installed_world()
    try:
        narrower = []
        for obj, name, real, double in pairs:
            if not (callable(real) and callable(double)) or inspect.ismodule(real):
                continue                                  # a module object, not a door
            want, _ = _keywords(real)
            got, var_kw = _keywords(double)
            missing = () if var_kw else sorted(want - got)
            slots_real, slots_double = _positional(real), _positional(double)
            short = (slots_double != -1 and slots_real != -1
                     and slots_double < slots_real)
            if missing or short:
                where = f"{getattr(obj, '__name__', obj)}.{name}"
                why = []
                if missing:
                    why.append("drops " + ", ".join(missing))
                if short:
                    why.append(f"takes {slots_double} positional, real takes {slots_real}")
                narrower.append(f"{where}: " + "; ".join(why))
        assert not narrower, (
            "these bench doubles cannot stand in for the real door — a live caller "
            "using the dropped parameter dies inside a swallowed except:\n  "
            + "\n  ".join(narrower))
    finally:
        world.uninstall()


@pytest.mark.asyncio
async def test_the_notify_double_takes_the_call_chat_watch_actually_makes():
    """The exact call that went dark, as the sweep makes it."""
    from app import notify
    world = W.World()
    world.install(chat_brain=W.ScriptedBrain([], world, "chat"),
                  task_brain=W.ScriptedBrain([], world, "task"))
    try:
        await notify.notify("💬 Teams\nsomebody said something", "teams",
                            urgency="direct", considered=True,
                            keys=("chat:one", "chat:two"))
    finally:
        world.uninstall()
    assert world.pushes and "somebody said something" in world.phone_text()


def test_the_doubles_come_back_out_when_a_run_dies_before_its_first_step():
    """`install` then `assert_sandboxed` used to sit outside the try, so a
    breach — or any other early raise — left the bench's notify wired into the
    live process for as long as it kept running."""
    from app import notify, store
    real = notify.notify
    was = store.DB_PATH
    world = W.World()
    try:
        world.install(chat_brain=W.ScriptedBrain([], world, "chat"),
                      task_brain=W.ScriptedBrain([], world, "task"))
        store.DB_PATH = (__import__("pathlib").Path(__file__).resolve().parent.parent
                         / "data" / "asta.db")
        with pytest.raises(W.SandboxBreach):
            world.assert_sandboxed()
    finally:
        store.DB_PATH = was
        world.uninstall()
    assert notify.notify is real, (
        "a bench double left behind swallows every push Asta tries to send him")
