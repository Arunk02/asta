"""Every turn ends in a message to Arun — including the turns that fail.

From a real WhatsApp thread, 30 July:

    13:10  Arun   Yes try help me to understand how vts cams works
    13:11  Asta   ⏳ on it — I'll send the answer here in a moment.
    13:19  Arun   What is this msg from Vinish from outlook…
    13:19  Asta   💬 still finishing the previous one — I'll answer this right after.
    13:22  Arun   Change the LLM model to Claude cli
    13:22  Asta   💬 still finishing the previous one — I'll answer this right after.
    16:02  Arun   whenever i ask questions it is saying its on it but not getting
                  any feedback … feels not doing

Three hours, three messages, no answer. Four separate defects lined up, and each
one on its own would have been survivable:

  1. Copilot's monthly quota was exhausted, so every turn failed.
  2. The "one fresh retry" for a dead session was not bounded to one, so a turn
     that could never succeed retried forever and never returned.
  3. Nothing bounded a turn by wall clock, so "forever" really was forever.
  4. HybridSink only flushed on {"type": "done"} — which a FAILING turn never
     sends — so even when the failure was reported it was reported into a buffer
     nobody read.

The through-line is that delivery was conditional on success. These tests pin it
the other way round: the worse the turn goes, the more certain he is to hear.
"""

from __future__ import annotations

import asyncio

import pytest

from app import copilot_cli, main as main_mod


# --- the silence itself ------------------------------------------------------

class _Chat:
    """Stands in for the WhatsApp bridge's /send."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


def test_a_failed_turn_still_reaches_him_after_handoff():
    """THE bug. The turn ran long (so we acked and switched to pushing), then
    died. The error went into the buffer and the buffer was only ever flushed by
    a "done" the failure path does not send."""
    chat = _Chat()
    sink = main_mod.HybridSink(chat.send, "c1")
    sink.handoff()                                   # "⏳ on it" already went back

    async def go():
        await sink.send({"type": "error", "message": "Copilot CLI exited 1: quota"})
        await sink.close()                           # what the finally now guarantees

    asyncio.run(go())
    assert chat.sent, "he was told 'on it' and then never heard anything again"
    assert "quota" in chat.sent[0]


def test_a_paused_turn_reaches_him_too():
    """"⏸ Paused — been at this 10 min" is a note, and the pause path returns
    without a "done" as well. Same silence, different cause."""
    chat = _Chat()
    sink = main_mod.HybridSink(chat.send, "c1")
    sink.handoff()

    async def go():
        await sink.send({"type": "note", "text": "⏸ Paused — been at this 10 min."})
        await sink.close()

    asyncio.run(go())
    assert chat.sent and "Paused" in chat.sent[0]


def test_a_staged_send_reaches_him_on_a_phone_channel():
    """_present_staged_send only sends "done" when channel == "web". On WhatsApp
    the "can I send this?" draft was buffered and dropped — so a confirmation he
    was supposed to answer never arrived, and the turn sat waiting for a reply to
    a question he never saw."""
    chat = _Chat()
    sink = main_mod.HybridSink(chat.send, "c1")
    sink.handoff()

    async def go():
        await sink.send({"type": "delta", "text": "📤 Ready to send — can I send this?"})
        await sink.close()

    asyncio.run(go())
    assert chat.sent and "can I send this?" in chat.sent[0]


def test_closing_before_handoff_stays_in_band():
    """A quick turn answers through the HTTP reply the bridge is still holding.
    Pushing there as well would double-send every fast answer."""
    chat = _Chat()
    sink = main_mod.HybridSink(chat.send, "c1")

    async def go():
        await sink.send({"type": "delta", "text": "hello"})
        await sink.close()

    asyncio.run(go())
    assert chat.sent == [], "fast turns must not be delivered twice"
    assert "hello" in sink.text()


def test_closing_twice_does_not_double_send():
    chat = _Chat()
    sink = main_mod.HybridSink(chat.send, "c1")
    sink.handoff()

    async def go():
        await sink.send({"type": "delta", "text": "one"})
        await sink.close()
        await sink.close()

    asyncio.run(go())
    assert chat.sent == ["one"]


def test_the_telegram_sink_also_closes():
    chat = _Chat()
    sink = main_mod.PushSink(chat.send, "c1")

    async def go():
        await sink.send({"type": "delta", "text": "half an answer"})
        await sink.close()

    asyncio.run(go())
    assert chat.sent == ["half an answer"]


def test_the_web_sink_closes_harmlessly():
    """One conductor drives every channel, so close() has to exist everywhere —
    the socket already got each event as it happened."""
    asyncio.run(main_mod.Emitter(None, "c1").close())


# --- the retry storm ---------------------------------------------------------

class _FakeProc:
    """A CLI that exits non-zero having printed nothing — exactly how copilot
    behaves once the monthly quota is gone."""

    def __init__(self, spawned: list) -> None:
        self.returncode = 1
        self._spawned = spawned
        self.stdout = self
        self.stderr = self

    async def read(self, n: int = -1) -> bytes:
        return b""

    async def wait(self) -> int:
        return 1

    def kill(self) -> None:
        return


def _count_spawns(monkeypatch) -> list:
    spawned: list = []

    async def fake_exec(*cmd, **kw):
        spawned.append(cmd)
        return _FakeProc(spawned)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(copilot_cli, "available", lambda: True)

    async def no_prefetch(_text):
        return ""

    monkeypatch.setattr(copilot_cli, "_prefetch", no_prefetch)
    return spawned


def test_a_turn_that_cannot_succeed_retries_once_and_then_reports(monkeypatch):
    """The retry was guarded on the session key existing — but _session_id writes
    that key back whenever it is missing, so the guard was true again every time.
    An exhausted quota is not fixed by a fresh session, and it looped ~every 20
    seconds until someone noticed. Nobody noticed for three hours."""
    spawned = _count_spawns(monkeypatch)
    from app import store
    conv = store.create_conversation(model="copilot", workspace=None)

    with pytest.raises(RuntimeError):
        asyncio.run(copilot_cli.run_turn(conv, "anything", None))

    assert len(spawned) == 2, f"expected one attempt + one retry, got {len(spawned)}"


def test_the_failure_it_finally_raises_names_the_cause(monkeypatch):
    """He has to be able to act on it. "Copilot CLI exited 1" with no body is how
    a dead brain looks identical to a slow one."""
    _count_spawns(monkeypatch)
    from app import store
    conv = store.create_conversation(model="copilot", workspace=None)
    with pytest.raises(RuntimeError, match="Copilot CLI exited 1"):
        asyncio.run(copilot_cli.run_turn(conv, "anything", None))


def test_quota_wording_is_recognised_as_a_quota_failure():
    """Copilot's actual words, verbatim. This is what routes the turn to another
    brain instead of just failing, so the matching has to survive its phrasing."""
    exc = RuntimeError("Copilot CLI exited 1: You have exceeded your monthly quota")
    assert main_mod._is_quota_error(exc)


# --- the pre-fetch that ran before any clock started -------------------------

def test_a_wedged_teams_read_cannot_hold_the_turn(monkeypatch):
    """Reading Teams/Outlook happens BEFORE the brain is spawned, so it sat
    outside the CLI's own ceiling. A wedged browser there is indistinguishable
    from a thinking model: no output, no error, no end. Context is optional; the
    answer is not."""
    async def never(_text):
        await asyncio.sleep(3600)

    monkeypatch.setattr(copilot_cli, "_teams_activity_context", never)
    monkeypatch.setattr(copilot_cli, "_outlook_context", never)
    monkeypatch.setattr(copilot_cli, "PREFETCH_TIMEOUT", 0.05)

    async def go():
        return await copilot_cli._prefetch("any messages for me?")

    assert asyncio.run(go()) == ""


def test_prefetch_still_returns_context_when_it_is_available(monkeypatch):
    """The ceiling must not cost the feature — a healthy read still rides along."""
    async def teams(_text):
        return "TEAMS BLOCK"

    async def outlook(_text):
        return "OUTLOOK BLOCK"

    monkeypatch.setattr(copilot_cli, "_teams_activity_context", teams)
    monkeypatch.setattr(copilot_cli, "_outlook_context", outlook)
    out = asyncio.run(copilot_cli._prefetch("any mail?"))
    assert "TEAMS BLOCK" in out and "OUTLOOK BLOCK" in out


# --- the escape hatch he actually reached for --------------------------------

@pytest.mark.parametrize("said,want", [
    ("Change the LLM model to Claude cli", "claude_cli"),   # his exact words
    ("change the model to claude cli", "claude_cli"),
    ("switch the brain to copilot", "copilot"),
    ("use claude cli", "claude_cli"),
    ("switch to copilot", "copilot"),
    ("change to local", "local"),
    ("use the model copilot", "copilot"),
    ("set model to copilot", "copilot"),
])
def test_switching_brains_is_recognised_however_he_phrases_it(said, want):
    """With the brain dead, this was the one message that could have rescued the
    conversation — and because it did not match, it was queued BEHIND the dead
    brain and answered with "still finishing the previous one"."""
    asked = main_mod._model_request(said)
    assert asked, f"unrecognised: {said!r}"
    assert main_mod._resolve_brain(asked) == want


@pytest.mark.parametrize("said", [
    "use it for the standup notes",
    "change the ticket status to done",
    "switch the branch to develop",
    "change the status to in progress",
    "use the same approach as yesterday",
])
def test_ordinary_sentences_are_not_mistaken_for_a_brain_switch(said):
    """Loosening the phrasing must not start eating real instructions. Answering
    "I don't have a brain called 'the ticket status to done'" is the same dropped
    message wearing a different hat — so loose phrasing must also name a brain
    that exists."""
    assert main_mod._model_request(said) == "", (
        f"{said!r} was misread as a request to change brains")


def test_an_unknown_brain_named_unambiguously_still_counts_as_asking():
    """"use frobnicator" names nothing real, but it is plainly a request to
    change brains — so it earns the list of real ones, not a model turn."""
    assert main_mod._model_request("use frobnicator") == "frobnicator"


def test_every_brain_in_the_registry_is_switchable_by_name():
    """"make the model selectable" has to mean ALL of them — the paid CLIs, the
    API-key models, and the free local one. A brain that exists but cannot be
    named is not selectable, and the registry is the only list that counts."""
    from app import agent as agent_mod
    for name in agent_mod.model_registry():
        assert main_mod._resolve_brain(name) == name, f"{name} cannot be selected by name"
        assert main_mod._model_request(f"use {name}") == name


def test_switching_is_wired_once_for_every_channel():
    """WhatsApp, Telegram and the web all reach _dispatch, so the phrasing fix
    lands on all three at once. Pinned because the alternative — a per-channel
    copy — is exactly how the two-brains-disagreeing bugs start."""
    import inspect
    src = inspect.getsource(main_mod._dispatch)
    assert "_model_request" in src
    for fn in (main_mod.api_wa_incoming, main_mod._telegram_turn):
        assert "_dispatch" in inspect.getsource(fn), (
            f"{fn.__name__} does not route through the shared dispatcher")


# --- one assistant, whichever brain is answering -----------------------------

def test_both_cli_brains_get_the_same_live_context():
    """Copilot pre-fetched Teams/Outlook and Claude did not, so "any messages for
    me?" read a real inbox on one brain and guessed on the other. Same assistant,
    same rules — the difference between brains should be who thinks, not what
    they can see."""
    import inspect
    from app import claude_cli
    assert "_prefetch" in inspect.getsource(claude_cli.run_turn)
    assert "_prefetch" in inspect.getsource(copilot_cli.run_turn)


def test_an_exhausted_brain_stops_being_the_default(monkeypatch):
    """`available()` only ever asked "is it installed". Copilot's monthly quota
    was gone and it was still handed every new conversation — so the switch had
    to be made by hand, per conversation, to a brain that could not answer any of
    them."""
    from app import agent as agent_mod
    monkeypatch.setattr(agent_mod, "available", lambda n: n in ("copilot", "claude_cli"))
    assert agent_mod.default_chat_model() == "copilot"      # office-paid goes first
    agent_mod.mark_quota_down("copilot")
    assert agent_mod.default_chat_model() == "claude_cli"   # …while it can answer


def test_an_exhausted_brain_comes_back_on_its_own(monkeypatch):
    """Nobody tells us when a monthly pool resets, so writing it off permanently
    would need a human to notice and undo it. It is retried once the cooldown
    passes instead."""
    from app import agent as agent_mod
    monkeypatch.setattr(agent_mod, "available", lambda n: True)
    agent_mod.mark_quota_down("copilot")
    assert agent_mod.quota_down("copilot")
    monkeypatch.setitem(agent_mod.QUOTA_COOLDOWN, "copilot", 0)
    assert not agent_mod.quota_down("copilot")


def test_the_picker_says_why_a_brain_is_greyed_out(monkeypatch):
    """"Copilot: unavailable" with no reason reads like a bug. Out of quota is a
    fact he can act on — or decide to wait out."""
    from app import agent as agent_mod
    monkeypatch.setattr(agent_mod, "available", lambda n: True)
    agent_mod.mark_quota_down("copilot")
    entry = agent_mod.model_registry()["copilot"]
    assert entry["available"] is False and "quota" in entry["detail"]


def test_one_table_records_quota_for_every_brain():
    """main.py used to write its own key that nothing else read — which is the
    whole reason a dead brain stayed the default. Same function, one source."""
    import inspect
    src = inspect.getsource(main_mod._cli_fallback)
    assert "mark_quota_down" in src


def test_the_exit_wait_is_bounded_in_every_cli_brain():
    """End-of-output is not end-of-process. Bounded in both, not just the one
    where it was noticed."""
    import inspect
    from app import claude_cli
    for mod in (copilot_cli, claude_cli):
        src = inspect.getsource(mod.run_turn).replace(" ", "")
        assert "wait_for(proc.wait()" in src, (
            f"{mod.__name__} can wait forever for a finished process to exit")


# --- the backstop ------------------------------------------------------------

def test_there_is_a_wall_clock_ceiling_on_any_single_turn():
    """Bound time, not steps — the rule the alerting already follows. It has to
    sit ABOVE the CLI's own limit so the brain's clearer error normally wins, and
    only catch the hangs nobody anticipated."""
    assert main_mod.TURN_CEILING > copilot_cli.turn_timeout(), (
        "the backstop must not pre-empt the brain's own, better-worded timeout")
    assert main_mod.TURN_CEILING <= 900, "a ceiling he'd never wait for is not a ceiling"
