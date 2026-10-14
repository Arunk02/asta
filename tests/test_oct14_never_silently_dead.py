"""14 Oct audit, P0: Asta must never be silently dead.

  · one operation held the one Teams lock past ten minutes 86 times in a week,
    and the incoming-call check waited behind it — rings missed, nothing said;
  · a test world held the live server 01:26–12:10 between two six-hourly checks;
  · with Claude limited and Copilot out, a tool-less brain "answered" his ask.
"""

import asyncio
import json
import time

import pytest

from app import health, incoming, store, teams_bridge


class _Page:
    def __init__(self, text="", hang=False):
        self.text, self.hang, self.closed = text, hang, False

    def is_closed(self):
        return self.closed

    async def evaluate(self, js, *a):
        if self.hang:
            await asyncio.sleep(3600)
        return self.text


@pytest.fixture
def pool(monkeypatch):
    page = _Page()
    monkeypatch.setitem(teams_bridge._POOL, "page", page)
    return page


# --- the lock says who has it ---------------------------------------------------------

def test_the_holder_is_named_and_cleared(monkeypatch, pool):
    async def pooled():
        return pool
    monkeypatch.setattr(teams_bridge, "_pooled_page", pooled)

    async def chat_read():
        async with teams_bridge.teams_page():
            return teams_bridge.holder()

    assert asyncio.run(chat_read()).endswith(".chat_read")
    assert teams_bridge.holder() == ""


def test_peek_page_is_the_open_page_or_nothing(monkeypatch, pool):
    assert teams_bridge.peek_page() is pool
    pool.closed = True
    assert teams_bridge.peek_page() is None
    monkeypatch.setitem(teams_bridge._POOL, "page", None)
    assert teams_bridge.peek_page() is None


def test_a_stuck_holder_is_cut_off_in_minutes_not_ten():
    assert teams_bridge.STUCK_SECONDS <= 300


# --- a ring is never behind the lock ----------------------------------------------------

def test_the_ring_check_runs_while_another_job_holds_the_lock(monkeypatch, pool):
    pool.text = "Vinish Kumar is calling you"
    monkeypatch.setattr(incoming, "capture_toast", lambda page: asyncio.sleep(0, result=""))

    async def scenario():
        await teams_bridge._lock.acquire()          # a chat read that never ends
        try:
            return await asyncio.wait_for(incoming.check_once(), 2)
        finally:
            teams_bridge._lock.release()

    call, _ = asyncio.run(scenario())
    assert call and "Vinish" in call.get("who", "")
    assert float(store.kv_get(incoming.CHECKED_KEY)) > time.time() - 5


def test_a_frozen_page_cannot_freeze_the_ring_check(monkeypatch, pool):
    pool.hang = True
    monkeypatch.setattr(incoming, "LOOK_TIMEOUT", 0.05)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(incoming.check_once())


def test_picking_up_does_not_wait_for_the_lock(monkeypatch, pool):
    from app import ops
    pool.text = "Komal Jayswal is calling you"

    async def answer(page, speak=False):
        return "Answered — listening only."

    monkeypatch.setattr(incoming, "answer", answer)

    async def scenario():
        await teams_bridge._lock.acquire()
        try:
            return await asyncio.wait_for(ops._call_answer(), 2)
        finally:
            teams_bridge._lock.release()

    assert "Answered" in asyncio.run(scenario())


# --- vitals: the fast check ------------------------------------------------------------

@pytest.fixture
def teams_on(monkeypatch):
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr(teams_bridge, "logged_in_once", lambda: True)
    monkeypatch.setattr(teams_bridge, "in_a_call", lambda: False)
    monkeypatch.setattr(incoming, "enabled", lambda: True)
    from app import agent
    monkeypatch.setattr(agent, "available", lambda b: True)
    monkeypatch.setattr(agent, "quota_down", lambda b: False)


def test_healthy_reads_as_healthy(teams_on, monkeypatch):
    now = time.time()
    monkeypatch.setattr(teams_bridge, "_last_read", lambda: now - 60)
    store.kv_set(incoming.CHECKED_KEY, str(now - 10))
    assert health.vitals(now=now) == {}


def test_a_test_world_in_the_live_server_is_vital(teams_on, monkeypatch):
    from app.workworld import world as W
    monkeypatch.setattr(W, "installed", lambda: True)
    assert "test_world" in health.vitals()


def test_stale_teams_and_a_stopped_ring_check_are_vital(teams_on, monkeypatch):
    now = time.time()
    monkeypatch.setattr(teams_bridge, "_last_read", lambda: now - 40 * 60)
    store.kv_set(incoming.CHECKED_KEY, str(now - 20 * 60))
    v = health.vitals(now=now)
    assert "40 min" in v["teams_read"] and "calls would be missed" in v["call_check"]


def test_a_ring_check_that_never_ran_since_startup_is_vital(teams_on, monkeypatch):
    now = time.time()
    monkeypatch.setattr(teams_bridge, "_last_read", lambda: now)
    store.kv_del(incoming.CHECKED_KEY)
    assert "call_check" in health.vitals(now=now, started=now - 10 * 60)


def test_no_brain_with_tools_is_vital(teams_on, monkeypatch):
    from app import agent
    monkeypatch.setattr(agent, "quota_down", lambda b: True)
    assert "brains" in health.vitals()


def test_vitals_say_it_once_after_two_checks_and_once_when_back(monkeypatch):
    told = []

    async def notify(text, *a, **k):
        told.append(text)

    from app import notify as notify_mod, wake
    monkeypatch.setattr(notify_mod, "notify", notify)
    class Done(BaseException):
        """Ends the loop: it catches Exception, so StopIteration would spin it."""

    states = iter([{"call_check": "x"}, {"call_check": "x"}, {"call_check": "x"}, {}, {}])

    def vitals(**k):
        for s in states:
            return s
        raise Done

    monkeypatch.setattr(health, "vitals", vitals)

    async def tick(_):
        return False

    async def no_wait(_):
        return None

    monkeypatch.setattr(wake, "sleep", tick)
    monkeypatch.setattr(health.asyncio, "sleep", no_wait)
    with pytest.raises(Done):
        asyncio.run(health.vitals_loop())
    assert len(told) == 2 and told[0].startswith("⚠️") and told[1].startswith("✅")


# --- no tool-capable brain: say so, redo later -------------------------------------------

def test_a_toolless_turn_says_so_and_is_queued(monkeypatch):
    from app import main
    sent = []

    class Out:
        async def send(self, payload):
            sent.append(payload)

    def boom(name):
        raise RuntimeError("stop after the note")

    monkeypatch.setattr(main.agent_mod, "get_model", boom)
    conv = store.create_conversation(model="local", workspace=None)
    asyncio.run(main._run_turn_streaming(Out(), conv, "send to Komal the review comments",
                                         "local", "whatsapp"))
    assert sent and "can't look anything up" in sent[0]["text"]
    queued = json.loads(store.kv_get(main._REDO_KEY))
    assert queued[0]["text"] == "send to Komal the review comments"


def test_queued_work_is_redone_when_a_brain_with_tools_is_back(monkeypatch):
    from app import main
    conv = store.create_conversation(model="local", workspace=None)
    main._queue_redo(conv["id"], "check booking H7QKN8NGN9R in prod", "whatsapp")
    ran, pushed = [], []

    async def dispatch(c, text, sink, channel):
        ran.append(text)
        return None

    async def wa_send(text, done=False):
        pushed.append(text)
        return True

    calls = {"n": 0}

    async def one_pass(_):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(main, "_dispatch", dispatch)
    monkeypatch.setattr(main.notify, "wa_send", wa_send)
    monkeypatch.setattr(main, "_tool_brain_up", lambda: True)
    monkeypatch.setattr(main.asyncio, "sleep", one_pass)
    monkeypatch.setattr(main, "_channel_model", lambda c: "claude_cli")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.redo_loop())
    assert ran == ["check booking H7QKN8NGN9R in prod"]
    assert pushed and "properly now" in pushed[0]
    assert json.loads(store.kv_get(main._REDO_KEY)) == []


# --- "yes post it" is a yes; a revised review replaces the one waiting -------------------

@pytest.mark.parametrize("text", ["yes post it", "post it", "Yes, post it now", "post the review comments",
                                  "yes go ahead", "proceed", "yes", "send it", "ok send", "sendd"])
def test_a_yes_may_carry_the_verb_it_approves(text):
    from app import main
    assert main._affirmation(text)[0]


@pytest.mark.parametrize("text", ["yes we dont want that tight coupling , yes no add a request change directly in the PR",
                                  "yes but shorter", "no", "send it to Vinish", "approve task 14"])
def test_a_yes_with_an_instruction_is_not_a_yes(text):
    from app import main
    assert not main._affirmation(text)[0]


def test_a_revised_review_replaces_the_one_waiting():
    from app import offers
    offers.drop_all()
    a = offers.staged_write("pr_review", {"pr": "1483", "repo": "telikos-booking-service",
                                          "action": "request_changes", "body": "first wording"},
                            "Request changes on PR #1483", "first", "Request changes?", kind="pr_write")
    b = offers.staged_write("pr_review", {"pr": "https://github.com/o/telikos-booking-service/pull/1483",
                                          "repo": "", "action": "request_changes", "body": "second wording"},
                            "Request changes on PR #1483", "second", "Request changes?", kind="pr_write")
    assert offers.pending().id == b.id != a.id
    assert not [w for w in offers.waiting() if w.id == a.id]


def test_a_different_review_still_queues():
    from app import offers
    offers.drop_all()
    a = offers.staged_write("pr_review", {"pr": "1483", "action": "request_changes", "body": "x"},
                            "s", "c", "q", kind="pr_write")
    offers.staged_write("pr_review", {"pr": "93", "action": "comment", "body": "y"},
                        "s2", "c2", "q2", kind="pr_write")
    assert offers.pending().id == a.id


# --- one yes: his asking, or his yes to what he was shown, is enough ---------------------

def _said(monkeypatch, text):
    from app import capabilities
    monkeypatch.setattr(capabilities, "said_this_turn", lambda: text)


@pytest.fixture
def posted(monkeypatch):
    from app import ops
    runs = []

    async def run(op):
        runs.append(op)
        return "Requested changes on PR #1483"

    monkeypatch.setattr(ops, "run", run)
    return runs


def test_add_a_request_change_in_his_words_posts_at_once(monkeypatch, posted):
    from app import agent, offers
    offers.drop_all()
    _said(monkeypatch, "yes we dont want that tight coupling , yes no add a request change directly in the PR")
    out = asyncio.run(agent.pr_review_post("1483", "request_changes", "coupling", repo="telikos-booking-service"))
    assert out.startswith("✅") and len(posted) == 1 and offers.pending() is None


def test_yes_post_it_posts_the_review_he_was_shown_and_clears_it(monkeypatch, posted):
    from app import agent, offers
    offers.drop_all()
    offers.staged_write("pr_review", {"pr": "1483", "repo": "telikos-booking-service",
                                      "action": "request_changes", "body": "coupling"},
                        "Request changes on PR #1483", "coupling", "Request changes?", kind="pr_write")
    _said(monkeypatch, "yes post it")
    out = asyncio.run(agent.pr_review_post("1483", "request_changes", "coupling v2", repo="telikos-booking-service"))
    assert out.startswith("✅") and len(posted) == 1
    assert offers.pending() is None, "the staged copy must not ask again"


def test_post_it_does_not_turn_a_comment_into_an_unshown_request_for_changes(monkeypatch, posted):
    from app import agent, offers
    offers.drop_all()
    _said(monkeypatch, "yes post it")
    out = asyncio.run(agent.pr_review_post("1483", "request_changes", "x", repo="telikos-booking-service"))
    assert posted == [] and "Staged" in out


def test_approval_still_needs_the_word(monkeypatch, posted):
    from app import agent, offers
    offers.drop_all()
    _said(monkeypatch, "post it")
    out = asyncio.run(agent.pr_review_post("1483", "approve", "", repo="telikos-booking-service"))
    assert posted == [] and "Staged" in out


def test_yes_update_and_send_sends_the_revision_to_the_waiting_draft(monkeypatch):
    from app import agent, loop
    cid = store.create_conversation(model="x", workspace=None)["id"]
    loop.stage(cid, {"type": "send", "to": "Komal Jayswal", "what": "old words", "channel": "teams"})
    _said(monkeypatch, "yes good, update the second line and send")
    assert agent._approves_waiting_draft("Komal Jayswal", cid)
    _said(monkeypatch, "update the second line but show me before you send")
    assert not agent._approves_waiting_draft("Komal Jayswal", cid)
    _said(monkeypatch, "yes send")
    assert not agent._approves_waiting_draft("Vinish Kumar", cid), "only the person the draft is for"


def test_asta_tools_load_up_front_not_behind_tool_search(monkeypatch):
    from app import claude_cli
    monkeypatch.delenv("ASTA_TOOL_SEARCH", raising=False)
    assert claude_cli._subprocess_env()["ENABLE_TOOL_SEARCH"] == "false"
    monkeypatch.setenv("ASTA_TOOL_SEARCH", "1")
    assert "ENABLE_TOOL_SEARCH" not in claude_cli._subprocess_env() or \
        claude_cli._subprocess_env().get("ENABLE_TOOL_SEARCH") != "false"


def test_a_chat_read_cannot_hold_the_browser_for_minutes(monkeypatch):
    """14 Oct: chat_watch.new_in held the Teams browser 666 s."""
    async def stuck(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(teams_bridge, "_read_history", stuck)
    monkeypatch.setattr(teams_bridge, "READ_TIMEOUT", 0.05)
    t = time.monotonic()
    with pytest.raises(RuntimeError, match="gave up"):
        asyncio.run(teams_bridge.read_history("Vinish Kumar", max_scrolls=0))
    assert time.monotonic() - t < 1


def test_a_normal_read_is_untouched(monkeypatch):
    async def fine(chat, since=None, limit=200, max_scrolls=0):
        return [{"text": "hi", "chat": chat}]

    monkeypatch.setattr(teams_bridge, "_read_history", fine)
    assert asyncio.run(teams_bridge.read_history("Vinish Kumar", max_scrolls=0))[0]["text"] == "hi"


# --- a restart never strands what was waiting in the queue ---------------------------------

def test_a_task_left_queued_by_a_restart_is_picked_up(monkeypatch):
    """14 Oct: ten colleague asks sat "queued" 5-8 hours, nothing running."""
    from app import tasks
    from app import notify as notify_mod

    async def quiet(*a, **k):
        return None

    monkeypatch.setattr(notify_mod, "notify", quiet)
    t = store.create_task("Vinish Kumar asked: LD in booking service, can you pick up?", "analysis", "p", None)
    store.update_task(t["id"], status="queued")
    picked = asyncio.run(tasks.recover_orphans())
    assert t["id"] in picked
    row = store.get_task(t["id"])
    assert row["status"] == "paused" and "queue" in row["error"]
    assert store.kv_get(f"task_resume_at:{t['id']}")


def test_a_task_waiting_in_this_process_is_left_alone(monkeypatch):
    from app import tasks
    from app import notify as notify_mod

    async def quiet(*a, **k):
        return None

    monkeypatch.setattr(notify_mod, "notify", quiet)
    t = store.create_task("still genuinely waiting", "analysis", "p", None)
    store.update_task(t["id"], status="queued")
    monkeypatch.setattr(tasks._gate, "queued_ids", lambda: [t["id"]])
    assert t["id"] not in asyncio.run(tasks.recover_orphans())
    assert store.get_task(t["id"])["status"] == "queued"


# --- a pasted link's card never "names" him; stale colleague asks are not answered late ----

JIRA_CARD = ("https://maersk-tools.atlassian.net/browse/BEPTELIKOS-10771\n\nJira Cloud\n\n"
             "BEPTELIKOS-10771: Mandatory field validation for downstream processing\n\n"
             "Assigned to Arunkumar K\n\nMedium\n\n|\n\nDONE\n\nComment\nEdit")


def test_a_link_card_assigned_to_him_is_not_a_mention():
    """14 Oct: Suraj pasted this into a sprint group; Asta treated it as an ask to Arun."""
    from app import chat_watch
    assert not chat_watch.mentions_him(JIRA_CARD)
    assert not chat_watch.addressed_to_him("Titans & Nexus : Sprint Backlog Refinement",
                                           "Suraj Prakash", JIRA_CARD)
    assert chat_watch.mentions_him("Arunkumar can you check this\n" + JIRA_CARD)
    assert chat_watch.mentions_him("Arun, the github build failed")


def test_a_stale_colleague_ask_left_in_the_queue_is_closed_not_answered(monkeypatch):
    from app import responder, tasks
    from app import notify as notify_mod
    told = []

    async def notify(text, *a, **k):
        told.append(text)

    monkeypatch.setattr(notify_mod, "notify", notify)
    old = store.create_task("Suraj Prakash asked: Suraj Prakash…", "analysis", "p", None)
    store.update_task(old["id"], status="queued",
                      created_at=time.time() - (responder.MAX_AGE_MINUTES + 30) * 60)
    store.kv_set(f"responder_task:{old['id']}", "teams-chat|Suraj Prakash|ask|x")
    mine = store.create_task("Add unit tests to PR 1482", "code", "p", None)
    store.update_task(mine["id"], status="queued", created_at=time.time() - 8 * 3600)
    picked = asyncio.run(tasks.recover_orphans())
    assert store.get_task(old["id"])["status"] == "superseded" and old["id"] not in picked
    assert mine["id"] in picked, "his own requests are always picked back up"
    assert any("Not answered" in t and f"#{old['id']}" in t for t in told)


def test_redo_runs_a_skipped_task_again(monkeypatch):
    from app import tasks
    spawned = []

    def spawn(title, prompt, kind="analysis", workspace=None, teams_chat="", **k):
        spawned.append((title, kind, teams_chat))
        return {"id": 9999, "title": title}

    monkeypatch.setattr(tasks, "spawn", spawn)
    t = store.create_task("Vinish Kumar asked: LD in booking service?", "analysis", "brief", None, "Vinish Kumar")
    store.update_task(t["id"], status="superseded")
    store.kv_set(f"responder_task:{t['id']}", "teams-chat|Vinish Kumar|ask|x")
    assert tasks.redo(t["id"])["id"] == 9999
    assert spawned == [("Vinish Kumar asked: LD in booking service?", "analysis", "Vinish Kumar")]
    assert store.kv_get("responder_task:9999")
    store.update_task(t["id"], status="running")
    with pytest.raises(ValueError):
        tasks.redo(t["id"])


# --- his "dont send any message to anyone" is a switch; a tool-less brain never answers ----

@pytest.mark.parametrize("text,want", [
    ("dont send any message to anyone", True), ("stop sending messages", True),
    ("please do not reply to anyone for now", True), ("you can send again", False),
    ("resume sending", False), ("dont send it to Vinish", None), ("stop", None),
])
def test_his_words_switch_the_hold(text, want):
    from app import main
    assert main._hold_command(text) is want


def test_while_held_nothing_goes_on_its_own(monkeypatch):
    from app import authority, guardrails, steward
    monkeypatch.setattr(guardrails, "section", lambda name: "- Vinish Kumar")
    monkeypatch.setenv("ASTA_SAY_WITHOUT_YES", "1")
    steward.hold_sends(True)
    try:
        assert not steward.say_without_yes()
        assert not authority.auto_reply_to("Vinish Kumar")
        with pytest.raises(PermissionError):
            asyncio.run(teams_bridge.send_automatic("Vinish Kumar", "ok bro"))
    finally:
        steward.hold_sends(False)
    assert steward.say_without_yes()


def test_a_toolless_turn_gives_the_note_and_no_answer(monkeypatch):
    from app import main
    sent = []

    class Out:
        async def send(self, payload):
            sent.append(payload)

    def must_not_run(name):
        raise AssertionError("the tool-less model must not answer")

    monkeypatch.setattr(main.agent_mod, "get_model", must_not_run)
    conv = store.create_conversation(model="local", workspace=None)
    asyncio.run(main._run_turn_streaming(Out(), conv, "dont send any message to anyone",
                                         "local", "whatsapp"))
    assert sent[0]["type"] == "note" and "can't look anything up" in sent[0]["text"]
    assert sent[-1]["type"] == "done"


# --- "nothing needed from you" goes to the digest, never his pocket -------------------------

def test_an_fyi_never_reaches_his_phone(monkeypatch):
    from app import digest, notify as notify_mod
    pushed, digested = [], []

    async def wa(text, done=False):
        pushed.append(text)
        return True

    monkeypatch.setattr(notify_mod, "wa_send", wa)
    monkeypatch.setattr(digest, "add", lambda text, **k: digested.append(text))
    out = asyncio.run(notify_mod.notify("· 💬 Teams — FYI, nothing needed from you (2):\n• x",
                                        "teams", urgency="fyi"))
    assert out.get("digested") and digested and pushed == []


def test_the_nothing_needed_senders_use_fyi():
    import inspect
    from app import answers, outlook
    assert 'urgency="direct" if wants_him else "fyi"' in inspect.getsource(teams_bridge)
    assert 'urgency="direct" if wants_him else "fyi"' in inspect.getsource(outlook)
    src = inspect.getsource(answers)
    assert src.count('urgency="fyi"') >= 2
