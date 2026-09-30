"""The interactive review session: the change in request order, in IntelliJ,
with his questions answered and his corrections kept — then applied on his word.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import store

DIFF = """diff --git a/src/main/java/x/BookingController.java b/src/main/java/x/BookingController.java
+++ b/src/main/java/x/BookingController.java
@@ -40,6 +42,9 @@ class BookingController
+    @PostMapping("/bookings/{id}/priority")
+    public ResponseEntity<Void> setPriority(@PathVariable String id) {
diff --git a/src/main/java/x/PriorityService.java b/src/main/java/x/PriorityService.java
+++ b/src/main/java/x/PriorityService.java
@@ -10,3 +10,12 @@ class PriorityService
+    public void set(String id) { repo.save(id); publisher.send(id); }
diff --git a/src/main/java/x/PriorityRepository.java b/src/main/java/x/PriorityRepository.java
+++ b/src/main/java/x/PriorityRepository.java
@@ -1,0 +5,4 @@
+    void save(String id);
"""


@pytest.fixture
def session(monkeypatch, tmp_path):
    from app import review, tasks, walkthrough
    root = tmp_path / "telikos-booking-service"
    for f in ("BookingController.java", "PriorityService.java", "PriorityRepository.java"):
        (root / "src/main/java/x").mkdir(parents=True, exist_ok=True)
        (root / "src/main/java/x" / f).write_text("\n".join(f"line {i}" for i in range(80)))
    t = store.create_task("Add transport priority API", "code", "p", "booking")
    tid = t["id"]
    store.update_task(tid, status="merged",
                      pr_urls="https://github.com/Maersk-Global/telikos-booking-service/pull/1432")

    async def gather(pr, workspace=""):
        return {"diff": DIFF, "title": "Add transport priority API",
                "target": "Maersk-Global/telikos-booking-service"}

    monkeypatch.setattr(review, "gather", gather)
    monkeypatch.setattr(walkthrough, "_workspace_roots", lambda ws: [tmp_path])
    opened, asked, refined = [], [], []

    async def idea(root, file, line):
        opened.append((file, line))
        return True

    async def ask(text):
        asked.append(text)
        if "Reply with ONLY this JSON" in text:
            return json.dumps({"overview": "POST hits the controller, the service saves and publishes.",
                               "steps": [
                                   {"file": "src/main/java/x/BookingController.java", "line": 42,
                                    "title": "New endpoint", "explain": "Adds POST priority."},
                                   {"file": "src/main/java/x/PriorityService.java", "line": 10,
                                    "title": "Service", "explain": "Saves then publishes."},
                                   {"file": "src/main/java/x/PriorityRepository.java", "line": 5,
                                    "title": "Repository", "explain": "New save method."}]})
        return "Because the id can be absent when the booking is new."

    async def refine(task_id, feedback):
        refined.append((task_id, feedback))
        return f"Task #{task_id}: continuing the open PR with your feedback."

    spoken = []

    async def say_aloud(text):
        spoken.append(text)

    monkeypatch.setattr(walkthrough, "say_aloud", say_aloud)
    monkeypatch.delenv("ASTA_WALKTHROUGH_VOICE", raising=False)
    monkeypatch.setattr(walkthrough, "open_in_idea", idea)
    monkeypatch.setattr(walkthrough, "_ask", ask)
    monkeypatch.setattr(tasks, "refine", refine)

    class S:
        pass
    s = S()
    s.tid, s.opened, s.asked, s.refined, s.cid = tid, opened, asked, refined, "conv-1"
    s.spoken = spoken
    s.say = lambda text: asyncio.run(walkthrough.handle(s.cid, text))
    s.start = lambda: asyncio.run(walkthrough.start(s.cid, f"task {tid}"))
    return s


@pytest.mark.parametrize("text,target", [
    ("walk me through task 126", "task 126"),
    ("open intellij and explain the code changes of task #126 one by one", "task 126"),
    ("review session for https://github.com/Maersk-Global/telikos-booking-service/pull/1432",
     "https://github.com/Maersk-Global/telikos-booking-service/pull/1432"),
    ("what changed in task 126", ""), ("walk the dog", ""),
])
def test_what_starts_a_walkthrough(text, target):
    from app import walkthrough
    assert walkthrough.wants_to_start(text) == target


def test_it_starts_at_the_entry_point_and_opens_intellij_there(session):
    out = session.start()
    assert "3 steps" in out and "1/3 · New endpoint" in out and "BookingController.java:42" in out
    assert session.opened == [("src/main/java/x/BookingController.java", 42)]


def test_next_back_and_again_move_intellij_with_it(session):
    session.start()
    assert "2/3 · Service" in session.say("next")
    assert "1/3" in session.say("back")
    assert "1/3" in session.say("again")
    assert session.opened[-1] == ("src/main/java/x/BookingController.java", 42)


def test_a_question_is_answered_about_this_step(session):
    session.start()
    out = session.say("why is the id optional here?")
    assert "id can be absent" in out
    assert "BookingController.java:42" in session.asked[-1], "asked about THIS step's code"


def test_a_correction_is_noted_not_acted_on(session):
    session.start()
    session.say("next")
    out = session.say("this should publish only after the save commits")
    assert "Noted for step 2" in out and not session.refined
    from app import walkthrough
    assert walkthrough.get(session.cid)["notes"][0]["file"].endswith("PriorityService.java")


def test_done_lists_the_notes_and_apply_continues_the_same_task(session):
    session.start()
    session.say("rename setPriority to updatePriority")
    out = session.say("done")
    assert "1 note(s)" in out and "updatePriority" in out and "apply" in out.lower()
    applied = session.say("apply")
    assert "continuing" in applied
    tid, spec = session.refined[0]
    assert tid == session.tid and "BookingController.java:42" in spec and "updatePriority" in spec
    from app import walkthrough
    assert walkthrough.get(session.cid) is None


def test_notes_kept_for_later_can_be_applied_later(session):
    from app import walkthrough
    session.start()
    session.say("add a null check on id")
    session.say("done")
    assert "Kept" in session.say("later")
    assert walkthrough.notes_for(session.tid)
    out = asyncio.run(walkthrough.apply_later("conv-2", f"apply review notes for task {session.tid}"))
    assert "continuing" in out and session.refined


def test_the_end_of_the_steps_is_the_end_of_the_session(session):
    session.start()
    session.say("next")
    session.say("next")
    assert "no changes noted" in session.say("next")


def test_an_analysis_task_has_nothing_to_walk(session):
    from app import walkthrough
    t = store.create_task("look into x", "analysis", "p", "booking")
    out = asyncio.run(walkthrough.start("c", f"task {t['id']}"))
    assert "made no code change" in out


def test_when_the_model_fails_the_diff_order_is_still_walked(session, monkeypatch):
    from app import walkthrough

    async def broken(text):
        return "sorry"

    monkeypatch.setattr(walkthrough, "_ask", broken)
    out = session.start()
    assert "3 steps" in out


def test_intellij_lands_on_the_line_the_change_added():
    from app import walkthrough
    diff = ("+++ b/helm/qa-values.yml\n@@ -84,3 +84,3 @@\n   KEY: a\n-  TMS_TOPIC: v10\n"
            "+  TMS_TOPIC: v11\n   SECURITY_PROTOCOL: x\n")
    added = walkthrough._added_lines(diff)
    assert added == {"helm/qa-values.yml": [85]}
    assert walkthrough._snap(86, added["helm/qa-values.yml"]) == 85



@pytest.mark.parametrize("text,voice", [
    ("walk me through task 126 and speak", True), ("explain the changes of task 126 aloud", True),
    ("walk me through task 126", False)])
def test_asking_for_voice(text, voice, monkeypatch):
    from app import walkthrough
    monkeypatch.delenv("ASTA_WALKTHROUGH_VOICE", raising=False)
    assert walkthrough.wants_voice(text) is voice


def test_a_spoken_walkthrough_says_each_step_and_answer(session):
    from app import walkthrough

    async def run():
        out = await walkthrough.start(session.cid, f"task {session.tid}", voice=True)
        await asyncio.sleep(0)
        nxt = await walkthrough.handle(session.cid, "next")
        await asyncio.sleep(0)
        ans = await walkthrough.handle(session.cid, "why is the id optional here?")
        await asyncio.sleep(0)
        return out, nxt, ans

    out, nxt, ans = asyncio.run(run())
    assert "Speaking each step" in out
    assert "Step 1. New endpoint" in session.spoken[0] and "POST hits the controller" in session.spoken[0]
    assert session.spoken[1].startswith("Step 2. Service")
    assert "id can be absent" in session.spoken[2]


def test_mute_and_unmute_mid_session(session):
    from app import walkthrough

    async def run():
        await walkthrough.start(session.cid, f"task {session.tid}", voice=True)
        await asyncio.sleep(0)
        muted = await walkthrough.handle(session.cid, "mute")
        await walkthrough.handle(session.cid, "next")
        await asyncio.sleep(0)
        on = await walkthrough.handle(session.cid, "voice on")
        return muted, on

    muted, on = asyncio.run(run())
    assert "Voice off" in muted and "Speaking" in on
    assert len(session.spoken) == 1, "nothing spoken while muted"


def test_silent_by_default(session):
    session.start()
    assert session.spoken == []


# --- 30 Sep: his feedback on a colleague's draft went to an open walkthrough -------------

def test_feedback_on_a_waiting_draft_is_never_taken_by_a_walkthrough(session):
    from app import walkthrough
    session.start()
    feedback = ("provide date as well kind of complete track when it received from booking "
                "and what billing consumed")
    assert not walkthrough.takes(session.cid, feedback, draft_waiting=True)
    assert walkthrough.takes(session.cid, feedback, draft_waiting=False), \
        "with no draft waiting it is a question for the session"


def test_a_yes_goes_to_the_draft_the_session_keeps_its_own_words(session):
    from app import walkthrough
    session.start()
    assert not walkthrough.takes(session.cid, "yes", draft_waiting=True, affirms=True)
    assert walkthrough.takes(session.cid, "next", draft_waiting=True)
    assert walkthrough.takes(session.cid, "done", draft_waiting=True)


def test_nothing_is_taken_without_a_session():
    from app import walkthrough
    assert not walkthrough.takes("no-session", "next", draft_waiting=False)
    assert walkthrough.takes("no-session", "walk me through task 126", draft_waiting=True)


def test_an_idle_walkthrough_closes_itself(session, monkeypatch):
    import time as _t
    from app import walkthrough
    session.start()
    assert walkthrough.get(session.cid)
    later = _t.time() + walkthrough.IDLE_SECONDS + 5
    monkeypatch.setattr(_t, "time", lambda: later)
    assert walkthrough.get(session.cid) is None
    assert not walkthrough.takes(session.cid, "why is this here?", draft_waiting=False)
