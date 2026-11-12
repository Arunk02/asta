"""12 Nov: a colleague's "that X thing" is resolved against the work they share
with Arun, and a guess is never told to them as "merged"."""
from __future__ import annotations

import json
import time

from app import answers, main, offers, shared_work, store, tasks


def _task(title, prompt="do it", chat="", pr="", state="MERGED", age_h=10.0, status="merged"):
    t = store.create_task(title, "code", prompt, None, chat)
    fields = {"status": status}
    if pr:
        fields.update(pr_urls=f"booking: {pr}", pr_state=state)
    store.update_task(t["id"], **fields)
    with store._connect() as conn:
        conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (time.time() - age_h * 3600, t["id"]))
    return t["id"]


def _link(chat, text, minutes_ago=30, key=None):
    store.save_teams_messages([{"key": key or f"{chat}:{text}", "chat": chat, "sender": chat,
                                "text": text, "sent_at": time.time() - minutes_ago * 60}])


PR_A = "https://github.com/acme/booking/pull/2101"
PR_B = "https://github.com/acme/booking/pull/2107"


def test_rows_come_from_their_chat_and_the_tasks_that_name_them(monkeypatch):
    calls = []
    monkeypatch.setattr(shared_work, "pr_meta",
                        lambda o, r, n: calls.append(n) or {"title": "Avro enums to strings", "state": "OPEN"})
    _task("Skip tracking call and vessel ETA check for one-way empty orders",
          prompt="Priya asked: skip the tracking call", pr=PR_A)
    _task("Unrelated change", prompt="someone else", pr="https://github.com/acme/booking/pull/2999")
    _link("Priya Nair", f"{PR_B} - review and approve it")
    rows = shared_work.rows("Priya Nair", "Priya Nair")
    labels = [r["label"] for r in rows]
    assert "booking PR 2101" in labels and "booking PR 2107" in labels
    assert "booking PR 2999" not in labels
    assert calls == ["2107"], "gh only for the link nothing local knew"
    shared_work.rows("Priya Nair", "Priya Nair")
    assert calls == ["2107"], "the title is cached"


def test_old_work_and_their_own_questions_are_not_shared_work():
    _task("Old skip change", prompt="Priya asked", pr=PR_A, age_h=24 * 20)
    asked = _task("Priya asked: is it merged", prompt="Priya Nair is asking", status="done")
    store.kv_set(f"responder_task:{asked}", "teams|Priya|ask|x")
    assert shared_work.rows("Priya Nair", "Priya Nair") == []


def _rows(*pairs):
    return [{"label": label, "title": title, "url": f"u{i}", "state": "MERGED", "task": 0}
            for i, (label, title) in enumerate(pairs)]


def test_their_words_resolve_to_the_shared_pr_that_covers_them():
    rows = _rows(("booking PR 2101", "Skip tracking call + vessel ETA validation for one-way empty orders"),
                 ("booking PR 2107", "Avro enum fields to plain strings"))
    res = shared_work.resolve("that vessel ETA thing for one way empty, merged?", rows, "Priya Nair")
    assert res["resolved"]["label"] == "booking PR 2101"
    assert res["conf"] >= 0.5


def test_an_initialism_matches_the_words_it_stands_for():
    rows = _rows(("booking PR 2101", "Skip tracking call for one-way empty orders"),
                 ("booking PR 2107", "Avro enum fields to plain strings"))
    assert shared_work.resolve("no tracking call for OWE", rows)["resolved"]["label"] == "booking PR 2101"


def test_two_equal_fits_are_candidates_not_an_answer():
    rows = _rows(("booking PR 2101", "Skip tracking call for one-way empty orders"),
                 ("booking PR 2109", "Arrival check for one-way empty orders"))
    res = shared_work.resolve("that one way empty thing merged?", rows)
    assert res["resolved"] is None and len(res["candidates"]) == 2
    assert shared_work.must_ask("that one way empty thing merged?", res)
    q = shared_work.question(res["candidates"])
    assert "booking PR 2101" in q and "booking PR 2109" in q and q.endswith("?")


def test_a_named_pr_is_not_a_vague_pointer():
    assert not shared_work.vague("is PR 2101 merged?")
    assert not shared_work.vague("that https://github.com/acme/booking/pull/2101 one merged?")
    assert shared_work.vague("that vessel ETA thing merged na?")


def test_the_brief_names_the_shared_work_and_the_rule():
    rows = _rows(("booking PR 2101", "Skip tracking call for one-way empty orders"))
    text = "that one way empty thing merged?"
    out = shared_work.block("Priya Nair", rows, shared_work.resolve(text, rows), text)
    assert "SHARED WORK" in out and "booking PR 2101" in out
    assert "GUESS" in out and "most likely mean booking PR 2101" in out
    empty = shared_work.block("Priya Nair", [], {"resolved": None, "candidates": []}, text)
    assert "GUESS" in empty
    assert shared_work.block("Priya Nair", [], {}, "is PR 9 merged?") == ""


def _remember(task_id, text, rows):
    shared_work.remember(task_id, text, shared_work.resolve(text, rows))


def test_merged_on_a_guess_becomes_the_question():
    rows = [{"label": "booking PR 2101", "title": "Skip tracking call for one-way empty orders",
             "url": PR_A, "state": "MERGED", "task": 0},
            {"label": "booking PR 2109", "title": "Arrival check for one-way empty orders",
             "url": "https://github.com/acme/booking/pull/2109", "state": "MERGED", "task": 0}]
    _remember(7, "that one way empty thing merged?", rows)
    q = shared_work.check_draft(7, "Found PR 2203 by title search", "Yeah merged bro")
    assert "booking PR 2101" in q and "booking PR 2109" in q and q.endswith("?")
    assert shared_work.check_draft(7, "", "Which one do you mean?") == ""


def test_merged_about_the_resolved_pr_stands():
    rows = [{"label": "booking PR 2101", "title": "Skip tracking call and vessel ETA check for one-way empty",
             "url": PR_A, "state": "MERGED", "task": 0}]
    _remember(8, "that vessel ETA thing for one way empty merged?", rows)
    assert shared_work.check_draft(8, "booking PR 2101 merged", "Yes, booking PR 2101 is merged") == ""
    assert shared_work.check_draft(8, "PR 2101 merged at 10:00", "Yes, merged") == ""
    assert shared_work.check_draft(8, "PR 3301 merged", "Yes, PR 3301 is merged") != ""


def test_a_named_pr_is_never_second_guessed():
    shared_work.remember(9, "is PR 2101 merged?", {"resolved": None, "candidates": []})
    assert shared_work.check_draft(9, "", "Yes, merged") == ""


def test_present_task_sends_the_question_not_the_guess(monkeypatch):
    sent = []

    async def _say(chat, text, **k):
        sent.append(text)
        return True

    async def _notify(*a, **k):
        return {}

    from app import chat_watch, notify, steward, understand
    monkeypatch.setattr(chat_watch, "_say", _say)
    monkeypatch.setattr(notify, "notify", _notify)
    monkeypatch.setattr(steward, "say_without_yes", lambda: True)
    monkeypatch.setattr(understand, "safe_question", lambda q, who: q)
    monkeypatch.setattr(answers, "moved_on", lambda chat, since: "")
    t = store.create_task("Priya asked: merged?", "analysis", "x", None, "Priya Nair")
    answers.remember_meta(t["id"], who="Priya Nair", need="merged?", chat="Priya Nair",
                          group=False, thread="teams:Priya Nair", source_text="that one way thing merged?")
    shared_work.remember(t["id"], "that one way thing merged?", {"resolved": None, "candidates": []})
    import asyncio
    asyncio.run(answers.present_task(t["id"], store.get_task(t["id"]),
                                     "ANALYSIS:\nfound PR 2203\nREPLY:\nYeah merged bro"))
    assert sent and "merged" not in sent[0].lower() and sent[0].endswith("?")


def test_draft_waiting_only_for_a_staged_answer_on_that_thread():
    from app import loop
    conv = store.create_conversation(model="claude_cli", workspace=None)
    store.kv_set("wa_conversation", conv["id"])
    t = store.create_task("ask", "analysis", "Priya Nair is asking: that thing merged?", None, "Priya Nair")
    store.update_task(t["id"], status="done")
    answers.remember_meta(t["id"], who="Priya Nair", need="", chat="Priya Nair", group=False,
                          thread="teams:Priya Nair")
    assert answers.draft_waiting("teams:Priya Nair", "for OWE") is None
    loop.stage(conv["id"], {"type": "answer", "what": "x", "to": "Priya Nair", "channel": "teams",
                            "task_id": t["id"]})
    assert answers.draft_waiting("teams:Priya Nair", "for OWE")["id"] == t["id"]
    assert answers.draft_waiting("teams:Other", "for OWE") is None
    assert answers.draft_waiting("teams:Priya Nair", "and BEPTELIKOS-99 too") is None


def test_review_asks_are_parsed_and_others_are_not():
    assert main._review_ask("review PR 3501") == ("3501", "")
    assert main._review_ask("pls review booking PR 3501")[1] == "telikos-booking-service"
    assert main._review_ask("review https://github.com/acme/booking/pull/12 again") == (
        "https://github.com/acme/booking/pull/12", "")
    assert main._review_ask("task 12 review PR 3401 fresh") == ("3401", "")
    assert main._review_ask("review task 12 PR 3401") == ("3401", "")
    for not_one in ("post PR 3501", "review PR 3501 and 3481", "did you review PR 3501?",
                    "review PR 3501 result", "what did the review of PR 3501 say", "review it"):
        assert main._review_ask(not_one) is None, not_one


def test_review_route_runs_review_pr_without_a_brain(monkeypatch):
    import asyncio
    from app import agent
    got = []

    async def _review_pr(pr, workspace="", repo=""):
        from app import capabilities
        got.append((pr, repo, capabilities.TURN_TEXT.get()))
        return "Task #9 — reviewing PR #3501"

    monkeypatch.setattr(agent, "review_pr", _review_pr)

    class Sink:
        def __init__(self):
            self.out = []

        async def send(self, m):
            self.out.append(m)

    conv = store.create_conversation(model="claude_cli", workspace=None)
    sink = Sink()
    asyncio.run(main._dispatch(conv, "review booking PR 3501", sink, "whatsapp"))
    assert got == [("3501", "telikos-booking-service", "review booking PR 3501")]
    assert any("reviewing PR #3501" in m.get("text", "") for m in sink.out)


def test_a_review_done_note_reads_the_staged_review_not_the_worker():
    offers.staged_write(
        "pr_review_inline",
        {"pr": "acme/booking#3481", "action": "comment", "body": "VERDICT: COMMENT",
         "comments": [{"path": "A.java", "line": 3, "body": "why?", "blocking": False}]},
        "🔎 Comment on PR #3481", "VERDICT: COMMENT\n\n? A.java:3 — asked as a question",
        "Post this review on PR #3481 as you?", kind="pr_write")
    t = {"title": "Review booking PR 3481: logging", "kind": "analysis"}
    tid = store.create_task(t["title"], "analysis", "x", None, "")["id"]
    note = tasks.staged_review_note(tid, t)
    assert note.startswith("Staged: COMMENT review — 1 inline comment(s), 0 blocking.")
    assert "1 asked as questions" in note
    assert tasks.staged_review_note(tid, {"title": "Something else PR 3481"}) == ""
