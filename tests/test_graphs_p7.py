"""The three P7 graphs — investigation, draft-and-send, follow-through.

Each is here for one property the daemon version could not have: it survives not
finishing in one go. So that is what these test — a thread stopped mid-way and
picked up by a DIFFERENT run, with what it had established intact.
"""

from __future__ import annotations

import asyncio

import pytest

from app.graph import draft_send, engine, follow_through, investigate


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ASTA_GRAPHS", "1")
    yield


# --- investigation ----------------------------------------------------------

def test_an_investigation_keeps_what_it_found_across_a_restart(monkeypatch):
    """A usage limit halfway through used to lose everything established. The
    findings are state now: the second run starts from what is known."""
    seen: list = []

    def checker(question, looks):
        seen.append(len(looks))
        if len(looks) == 0:
            return {"what": "temporal workflows", "found": ""}      # nothing yet
        return {"what": "the booking topic", "found": "3 stuck since 09:12"}

    monkeypatch.setattr(investigate, "CHECKER", checker)
    key = "q-restart"
    first = asyncio.run(engine.drive("investigate", key, investigate.build,
                                     {"question": "are prod bookings stuck?"}))
    assert first["verdict"] == "answered"
    assert "3 stuck since 09:12" in first["answer"]
    # The second look ran knowing about the first — that is the whole point.
    assert seen == [0, 1]


def test_an_investigation_that_needs_him_parks_at_one_question(monkeypatch):
    def checker(question, looks):
        return {"what": "the dashboard", "found": "ask him: which environment?"}

    monkeypatch.setattr(investigate, "CHECKER", checker)
    out = asyncio.run(engine.drive("investigate", "q-ask", investigate.build,
                                   {"question": "is it happening in prod?"}))
    assert out["waiting_at"] == "investigate"


def test_an_investigation_stops_rather_than_looking_for_ever(monkeypatch):
    monkeypatch.setattr(investigate, "CHECKER",
                        lambda q, looks: {"what": "nothing useful", "found": ""})
    out = asyncio.run(engine.drive("investigate", "q-exhaust", investigate.build,
                                   {"question": "why is it slow?"}))
    assert out["verdict"] == "exhausted"
    assert len(out["looks"]) == investigate.MAX_LOOKS


# --- draft and send ---------------------------------------------------------

def test_an_approval_that_arrives_before_a_restart_still_sends(monkeypatch):
    """13 September: his "approve" arrived moments before a restart and was lost
    twice over. Recorded first, applied second — so a different run sends it."""
    sent: list = []
    monkeypatch.setattr(draft_send, "WRITER", lambda s: "Bro, can you review 1440?")
    monkeypatch.setattr(draft_send, "SENDER",
                        lambda s: sent.append(s["body"]) or "delivered")
    monkeypatch.setattr(draft_send, "CONFIRMER", lambda s: "delivered")

    key = "d-restart"
    first = asyncio.run(engine.drive("draft", key, draft_send.build,
                                     {"to": "a colleague", "channel": "teams"}))
    assert first["waiting_at"] == "send" and sent == []

    # He approves — recorded — and the process dies before applying it.
    from app import store
    import json
    store.kv_set(f"graph_answer:draft:{key}", json.dumps({"approved": True}))

    out = asyncio.run(engine.resume("draft", key, draft_send.build))
    assert sent == ["Bro, can you review 1440?"]
    assert out["outcome"] == "sent"


def test_no_with_words_rewrites_and_no_alone_drops_it(monkeypatch):
    drafts: list = []
    sent: list = []

    def writer(state):
        drafts.append(state.get("answer", {}).get("text", ""))
        return "second attempt" if drafts[-1] else "first attempt"

    monkeypatch.setattr(draft_send, "WRITER", writer)
    monkeypatch.setattr(draft_send, "SENDER", lambda s: sent.append(s["body"]) or "ok")
    monkeypatch.setattr(draft_send, "CONFIRMER", lambda s: "ok")

    asyncio.run(engine.drive("draft", "d-again", draft_send.build,
                             {"to": "x", "channel": "teams"}))
    again = asyncio.run(engine.answer("draft", "d-again", draft_send.build,
                                      {"approved": False, "text": "too formal"}))
    assert again["waiting_at"] == "send", "it re-staged rather than sending"
    assert drafts == ["", "too formal"] and sent == []

    asyncio.run(engine.drive("draft", "d-drop", draft_send.build,
                             {"to": "x", "channel": "teams"}))
    out = asyncio.run(engine.answer("draft", "d-drop", draft_send.build,
                                    {"approved": False, "text": ""}))
    assert out["outcome"] == "dropped" and sent == []


def test_a_send_that_cannot_be_confirmed_is_not_reported_as_sent(monkeypatch):
    """"Sent" that only means "the function returned" is how a message nobody
    received became something he heard about from the person who never got it."""
    monkeypatch.setattr(draft_send, "WRITER", lambda s: "hello")
    monkeypatch.setattr(draft_send, "SENDER", lambda s: "queued")
    monkeypatch.setattr(draft_send, "CONFIRMER", lambda s: "")      # not there
    asyncio.run(engine.drive("draft", "d-unconfirmed", draft_send.build,
                             {"to": "x", "channel": "teams"}))
    out = asyncio.run(engine.answer("draft", "d-unconfirmed", draft_send.build,
                                    {"approved": True}))
    assert out["outcome"] == "failed"


# --- follow through ---------------------------------------------------------

def test_a_promise_warns_before_the_deadline_and_waits_for_him(monkeypatch):
    """After EOD it is a report. Four hours before, it is still actionable —
    and what he says back has to be waited for, across a restart."""
    import time
    told: list = []
    monkeypatch.setattr(follow_through, "WATCHER",
                        lambda p: {"state": "no review yet", "moved": False})
    monkeypatch.setattr(follow_through, "WARNER", lambda p: told.append(p["goal"]))
    monkeypatch.setattr(follow_through, "NUDGER", lambda p: told.append("nudged"))

    out = asyncio.run(engine.drive("promise", "p-1", follow_through.build,
                                   {"goal": "3 PRs merged", "urls": ["u"],
                                    "due_at": time.time() + 3600}))
    assert told == ["3 PRs merged"] and out["waiting_at"] == "deadline"

    done = asyncio.run(engine.answer("promise", "p-1", follow_through.build,
                                     {"text": "leave it, I'll do it myself"}))
    assert done["outcome"] == "his_call"


def test_a_stalled_promise_stages_a_nudge_and_never_sends_one(monkeypatch):
    staged: list = []
    monkeypatch.setattr(follow_through, "WATCHER",
                        lambda p: {"state": "still open", "moved": False})
    monkeypatch.setattr(follow_through, "NUDGER", lambda p: staged.append(p["person"]))
    out = asyncio.run(engine.drive("promise", "p-2", follow_through.build,
                                   {"goal": "3 PRs merged", "person": "a colleague",
                                    "due_at": 0}))
    assert staged == ["a colleague"] and out["nudges"] == 1
    assert "sent" not in out


def test_a_promise_that_came_true_closes_itself(monkeypatch):
    monkeypatch.setattr(follow_through, "WATCHER",
                        lambda p: {"state": "merged", "done": True})
    out = asyncio.run(engine.drive("promise", "p-3", follow_through.build,
                                   {"goal": "3 PRs merged", "due_at": 0}))
    assert out["outcome"] == "kept"


# --- the wiring, not just the shapes ----------------------------------------

def test_his_one_word_answer_reaches_the_parked_promise(monkeypatch):
    """A warning that asks "chase them or leave it?" and cannot hear the answer
    is a broadcast, not a question — the thread sits at its gate for ever."""
    import time

    from app import followup, frontdesk
    from app.graph import bindings

    row = followup.track("3 PRs merged", ["https://example.invalid/pr/1"],
                         person="a colleague", due_at=time.time() + 3600)
    monkeypatch.setattr(follow_through, "WATCHER",
                        lambda p: {"state": "no review yet", "moved": False})
    monkeypatch.setattr(follow_through, "WARNER", lambda p: None)
    monkeypatch.setattr(follow_through, "NUDGER", lambda p: None)

    parked = asyncio.run(engine.drive("promise", row["id"], follow_through.build,
                                      {"goal": row["goal"], "id": row["id"],
                                       "due_at": row["due_at"]}))
    assert parked["waiting_at"] == "deadline"
    assert frontdesk._promises_at_a_gate() == [int(row["id"])]

    said = frontdesk.answer_from_state("leave it")
    assert "Left it with you" in said
    # Recorded the moment he said it — the front desk is synchronous and must
    # not drive a thread — and applied by the next tick. A restart in between
    # changes nothing, which is the whole point of writing it down first.
    assert engine.answer_waiting("promise", row["id"]) == {"text": "leave it"}
    applied = asyncio.run(bindings.tick_promise(dict(row)))
    assert applied["outcome"] == "his_call"
    assert bindings.waiting_on("promise", row["id"]) == ""


def test_a_gate_answer_is_never_guessed_at_when_two_are_waiting(monkeypatch):
    """Rules only. Two parked promises and a one-word answer is a question back,
    not a coin toss — the same discipline as binding a message to a job."""
    import time

    from app import followup, frontdesk
    rows = [followup.track(f"promise {i}", [f"https://example.invalid/pr/{i}"],
                           due_at=time.time() + 3600) for i in (1, 2)]
    monkeypatch.setattr(follow_through, "WATCHER",
                        lambda p: {"state": "stuck", "moved": False})
    monkeypatch.setattr(follow_through, "WARNER", lambda p: None)
    for row in rows:
        asyncio.run(engine.drive("promise", row["id"], follow_through.build,
                                 {"goal": row["goal"], "id": row["id"],
                                  "due_at": row["due_at"]}))
    said = frontdesk.answer_from_state("leave it")
    assert "Which one" in said and all(f"#{r['id']}" in said for r in rows)


def test_the_promise_id_survives_the_state_schema():
    """LangGraph keeps only the keys a state schema names. `id` was undeclared,
    so it was dropped between the caller and the first node — and the watcher
    then looked up a promise that never arrived and reported it "gone", which
    reads exactly like a promise that was kept."""
    assert "id" in follow_through.Promise.__annotations__


def test_leave_it_closes_the_promise_so_it_never_warns_again(monkeypatch):
    """Live, 17 Sep: he said "leave it", the thread applied it and cleared its
    gate — and the follow-up row stayed OPEN. The next tick would have started a
    fresh run and warned him again, every fifteen minutes, about the thing he
    had just told it to drop."""
    import time

    from app import followup, frontdesk
    from app.graph import bindings

    row = followup.track("my own PR merged", ["https://example.invalid/pr/9"],
                         person="", due_at=time.time() + 3600)
    warned: list = []
    monkeypatch.setattr(bindings, "_watch",
                        lambda p: {"state": "open", "moved": False})

    async def warn(p):
        warned.append(p["goal"])

    monkeypatch.setattr(bindings, "_warn", warn)
    asyncio.run(followup.check_all())
    assert warned == ["my own PR merged"]

    assert "Left it with you" in frontdesk.answer_from_state("leave it")
    asyncio.run(followup.check_all())                 # applies "leave it"
    assert all(r["id"] != row["id"] for r in followup.list_open()), \
        "the promise he dropped is still being kept"

    asyncio.run(followup.check_all())                 # and nothing comes back
    assert warned == ["my own PR merged"]


def test_one_question_is_one_analysis_not_four(monkeypatch):
    """The first binding spawned an analysis and returned before it finished.
    `judge` saw nothing found, looked again, spawned again — one question would
    have become four analyses side by side. A look waits for its result, and a
    re-run look reuses the analysis it already started."""
    from app import store, tasks
    from app.graph import bindings

    spawned: list = []

    def spawn(title, prompt, kind, workspace, chat):
        tid = 900 + len(spawned)
        spawned.append(tid)
        store.create_task(title, kind, prompt, None)
        return {"id": tid}

    monkeypatch.setattr(tasks, "spawn", spawn)
    monkeypatch.setattr(store, "get_task",
                        lambda tid: {"status": "done", "result": "3 bookings stuck since 09:12"})

    out = asyncio.run(bindings.investigate_question("q-one", "are prod bookings stuck?"))
    assert out["verdict"] == "answered" and "3 bookings stuck" in out["answer"]
    assert len(spawned) == 1, f"one question became {len(spawned)} analyses"

    # A restart re-runs the look: it must find its analysis, not start another.
    found = asyncio.run(bindings._looker("are prod bookings stuck?", []))
    assert found["task"] == spawned[0] and len(spawned) == 1
