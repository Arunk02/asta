"""16 Oct, Vinish, 12:40–15:02. Eight things went wrong in one conversation:

1. "Looking into the PR details, bro" was put to him as a draft to approve;
2. "Bro, that cam's one" was pushed after he had already answered it;
3. task #346's answer to the first, vaguer reading arrived after #347 took over;
4. a draft said "no Jira create tool on my side" — as him;
5. "Vinish asked about create a story…";
6. #347's findings reached him as "Filed as needs_input again";
7. "can you check BEPTELIKOS-11443?" was folded into the CAMS schema task;
8. his 13:02 answer to #347 was lost when that leg paused for the usage limit.
"""

import asyncio
import json
import time

import pytest

from app import answers, chat_watch, store, tasks, threads


# 1 ------------------------------------------------------------------------------------
@pytest.mark.parametrize("line", ["Looking into the PR details, bro", "Let me look into this, bro",
                                  "Checking the build", "Got it, checking what our side needs for this"])
def test_asta_s_own_receipts_are_progress_lines(line):
    assert chat_watch.progress_only(line)


@pytest.mark.parametrize("line", ["Yes, could you share a bit more on this?", "hi Vinish, yes tell me"])
def test_a_question_is_not_a_progress_line(line):
    assert not chat_watch.progress_only(line)


def test_a_progress_line_in_his_conversation_is_not_a_draft(monkeypatch):
    shown = []

    async def present(**k):
        shown.append(k)
        return True

    async def no(*a, **k):
        return False

    monkeypatch.setattr(answers, "present", present)
    monkeypatch.setattr(chat_watch, "he_replied_since", no)
    monkeypatch.setattr(chat_watch, "in_his_exchange", lambda *a, **k: True)
    said = asyncio.run(chat_watch._say("Vinish Kumar", "Looking into the PR details, bro", since=time.time()))
    assert said is False and shown == []


# 2 ------------------------------------------------------------------------------------
def test_a_message_he_already_answered_is_not_news():
    assert asyncio.run(chat_watch._answered_already({"handled_by_him": True, "chat": "Vinish Kumar"}))


# 3 ------------------------------------------------------------------------------------
def test_a_newer_task_for_the_same_person_takes_over_the_draft(monkeypatch):
    sent = []

    async def notify(text, *a, **k):
        sent.append(text)

    from app import notify as n
    monkeypatch.setattr(n, "notify", notify)
    store.kv_set("wa_conversation", "phone-conv")
    old = store.create_task(title="Vinish Kumar asked: create a story", kind="analysis", prompt="x",
                            workspace="booking", teams_chat="Vinish Kumar")["id"]
    time.sleep(0.01)
    store.create_task(title="Update JSON schema for cams", kind="code",
                      prompt="Vinish Kumar (Teams, 16 Oct) asked: update the schema", workspace="booking")
    asyncio.run(answers.present(who="Vinish Kumar", need="create a story", chat="Vinish Kumar", group=False,
                                analysis="PR 25750 is merged", reply="is #25750 the one?", task_id=old))
    assert sent and "has taken it over" in sent[0] and "no reply drafted" in sent[0]


# 4 ------------------------------------------------------------------------------------
@pytest.mark.parametrize("reply,flag", [
    ("Can't create the story from here — no Jira create tool on my side, I'll raise it myself.", True),
    ("I'll create the story today.", False),
    ("I don't have access to prod logs", False),          # he may say that himself
    ("no access needed, it's in develop", False)])
def test_a_draft_that_talks_about_the_assistant_is_flagged(reply, flag):
    assert answers.talks_about_itself(reply) is flag


def test_the_brief_says_the_reply_is_his():
    assert "never mention tools" in answers.REPLY_FORMAT


# 5 ------------------------------------------------------------------------------------
def test_a_thing_to_do_reads_as_a_sentence():
    head = answers.render("Vinish", "Create a story for the schema updates", "", "x").splitlines()[0]
    assert head == "*Vinish* wants you to create a story for the schema updates."


# 6 ------------------------------------------------------------------------------------
def test_a_bare_sign_off_is_replaced_by_what_was_reported():
    tid = store.create_task(title="Update JSON schema for cams", kind="code", prompt="x", workspace="booking")["id"]
    store.kv_set(f"task_outcome:{tid}", json.dumps({
        "kind": "needs_input", "at": time.time(),
        "summary": "The vendored CAMS schema is byte-identical to master — no drift.",
        "questions": ["Start consuming CamsRailTerminalResponse, or a different file?"]}))
    out = tasks.with_reported(tid, "Filed as `needs_input` again — still waiting on scope confirmation.")
    assert "byte-identical to master" in out and "CamsRailTerminalResponse" in out


def test_a_full_answer_is_left_alone():
    tid = store.create_task(title="t", kind="code", prompt="x", workspace="booking")["id"]
    store.kv_set(f"task_outcome:{tid}", json.dumps({"kind": "needs_input", "at": time.time(), "summary": "s"}))
    long = "Finding: " + "detail " * 100
    assert tasks.with_reported(tid, long) == long


# 7 ------------------------------------------------------------------------------------
ELEVEN = "Bro, can you please check https://maersk-tools.atlassian.net/browse/BEPTELIKOS-11443?"


def test_a_new_ticket_is_not_a_follow_up_of_the_old_ask():
    t = {"prompt": "Vinish asked: create a story; schema PR https://github.com/x/y/pull/25750",
         "title": "Vinish Kumar asked: create a story"}
    assert answers._new_subject(ELEVEN, t, {"source_text": "They have updated the schema twice"})
    assert not answers._new_subject("Update the schema in booking service, For cams", t, {})


def test_a_new_ticket_is_not_folded_into_a_code_task_through_the_recent_chat(monkeypatch):
    store.create_task(title="Update JSON schema for cams", kind="code", workspace="booking",
                      prompt="apply https://github.com/Maersk-Global/API-JSON-Schema-Definitions/pull/25750")
    with store._connect() as conn:
        conn.execute("UPDATE tasks SET status='running'")
    recent = "Vinish: They have updated the schema twice https://github.com/Maersk-Global/API-JSON-Schema-Definitions/pull/25750/changes"
    own = threads.entities_in(ELEVEN)
    assert own and chat_watch._live_work("teams:Vinish Kumar", ELEVEN if own else f"{ELEVEN}\n{recent}") is None
    # …while "this PR, bro", which names nothing, still finds the task through the chat.
    assert chat_watch._live_work("teams:Vinish Kumar", f"this PR, bro\n{recent}") is not None


# 8 ------------------------------------------------------------------------------------
def test_a_resumed_step_carries_the_answer_it_was_delivering(monkeypatch):
    from app.graph import code_graph
    tid = store.create_task(title="Update JSON schema for cams", kind="code", prompt="x", workspace="booking")["id"]
    prompts = []

    async def leg(tid_, prompt, cwd, **k):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise RuntimeError("usage limit")          # paused seconds into the step
        return "done"

    monkeypatch.setattr(code_graph, "_has_session", lambda _t: True)
    monkeypatch.setattr(tasks, "_run_code_leg", leg)
    monkeypatch.setattr(tasks, "task_cwd", lambda *a: "/tmp")
    answer = "can't you fetch directly from main, copy the whole json and paste in our booking service?"
    with pytest.raises(RuntimeError):
        asyncio.run(code_graph._leg(tid, "plan", "fresh", effort="medium", resume_prompt=answer))
    asyncio.run(code_graph._leg(tid, "plan", "fresh", effort="medium", resume_prompt="ignored"))
    assert answer in prompts[0]
    assert code_graph.RESUME_PROMPT in prompts[1] and answer in prompts[1]
