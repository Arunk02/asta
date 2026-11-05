"""What Asta said to him stays in the conversation (5 Nov).

Synthetic replay of one evening: a read-only side turn told him where a field
was dropped, and only his phone ever saw it — the main brain then asked him to
choose a repo it did not know it had already named. Its next turn was cut at the
CLI ceiling, so the question it asked was not in the history either, and his
answer arrived four seconds after the turn waiting for it had died.

Synthetic: the service names, ids and wording are made up.
"""
from __future__ import annotations

import asyncio

import pytest

from app import asking, main, store, turn_budget


class Sink:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def _history(cid):
    return [(m["role"], m["content"]) for m in store.list_ui_messages(cid)]


def test_a_side_answer_lands_in_the_main_conversation(monkeypatch):
    from app import copilot_cli
    monkeypatch.setattr(copilot_cli, "mcp_cli_enabled", lambda: True)
    conv = store.create_conversation("copilot", None)
    store.add_ui_message(conv["id"], "user", "look at the missing date", {})

    async def fake_turn(sink, current, text, channel):
        store.add_ui_message(current["id"], "user", text, {})
        store.add_ui_message(current["id"], "assistant",
                             "The date is dropped inside svc-alpha's dispatcher.", {})

    monkeypatch.setattr(main, "_run_turn", fake_turn)

    async def go():
        side = main._start_side_turn(conv, "tell me the exact issue", Sink(), "whatsapp")
        assert side is not None
        await side

    asyncio.run(go())
    said = _history(conv["id"])
    assert ("user", "tell me the exact issue") in said, "his question, in his words"
    assert ("assistant", "The date is dropped inside svc-alpha's dispatcher.") in said
    assert not any("Recent conversation context" in c for _, c in said), \
        "the side turn's context scaffold is not his message"


def test_the_main_brain_is_told_what_the_side_turn_said(monkeypatch):
    conv = store.create_conversation("claude_cli", None)
    main._note_side_answer(conv["id"], "tell me the exact issue",
                           "The date is dropped inside svc-alpha's dispatcher.")
    seen = []

    class Cli:
        async def run_turn(self, c, text, on_delta):
            seen.append(text)
            return "ok"

    asyncio.run(main._run_turn_cli(Sink(), conv, "fix it there", Cli(), "claude_cli"))
    assert "dropped inside svc-alpha" in seen[0] and seen[0].rstrip().endswith("fix it there")
    asyncio.run(main._run_turn_cli(Sink(), conv, "and the tests", Cli(), "claude_cli"))
    assert seen[1] == "and the tests", "told once, then it is in the session"
    assert not any("separate read-only turn" in c for _, c in _history(conv["id"])), \
        "the note is for the brain, never stored as his words"


def test_a_turn_cut_at_the_ceiling_keeps_what_it_said():
    conv = store.create_conversation("claude_cli", None)

    class Cli:
        async def run_turn(self, c, text, on_delta):
            raise turn_budget.TurnStopped(
                turn_budget.Stop("ceiling", 300.0, 2.0,
                                 ["The drop is in svc-beta. Should the fix target svc-beta?"]),
                already_shown=True)

    with pytest.raises(turn_budget.TurnStopped):
        asyncio.run(main._run_turn_cli(Sink(), conv, "replicate and fix it", Cli(), "claude_cli"))
    rows = store.list_ui_messages(conv["id"])
    assert rows[-1]["role"] == "assistant"
    assert "Should the fix target svc-beta?" in rows[-1]["content"]


def test_an_answer_to_a_question_whose_turn_died_is_not_swallowed(monkeypatch):
    monkeypatch.setattr("app.notify.notify", lambda *a, **k: asyncio.sleep(0))
    asking._waiters.clear()

    async def go():
        asker = asyncio.create_task(asking.ask("Target svc-beta, or svc-alpha?", "chat"))
        await asyncio.sleep(0.05)
        qid = store.open_questions()[0]["id"]
        asker.cancel()                              # the turn hit its ceiling
        with pytest.raises(asyncio.CancelledError):
            await asker
        return qid

    qid = asyncio.run(go())
    assert not store.open_questions()
    assert not asking.answer(qid, "you just told me it is svc-alpha"), \
        "nobody is waiting: it must not report delivered"
    text = asking.orphan_reply(qid, "you just told me it is svc-alpha")
    assert "you just told me it is svc-alpha" in text
    assert "Target svc-beta, or svc-alpha?" in text
    assert asking.orphan_reply(qid + 99, "x") is None


def test_answer_n_to_a_dead_question_goes_on_to_the_brain(monkeypatch):
    monkeypatch.setattr("app.notify.notify", lambda *a, **k: asyncio.sleep(0))
    q = store.create_question("Target svc-beta, or svc-alpha?", "chat")
    store.close_question(q["id"], "", status="orphaned")
    conv = store.create_conversation("claude_cli", None)
    started = []

    def fake_start(c, text, sink, channel):
        started.append(text)
        return None

    monkeypatch.setattr(main, "_start_turn", fake_start)
    sink = Sink()
    asyncio.run(main._dispatch(conv, f"answer {q['id']} you just told me it is svc-alpha",
                               sink, "whatsapp", skip_new_gates=True))
    assert not any("Answer delivered" in (p.get("text") or "") for p in sink.sent)
    assert started and "you just told me it is svc-alpha" in started[0]
    assert "Target svc-beta, or svc-alpha?" in started[0]


def test_zero_log_lines_is_never_presented_as_absence():
    from app import debugkit
    out = debugkit.render_lines({"rows": [], "namespace": "sit", "start": 0, "end": 60,
                                 "query": '{app="svc"} |= "etaField"', "link": "http://g"})
    assert "Not proof of absence" in out and "exact enum" in out
