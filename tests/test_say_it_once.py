"""He says it once.

30 Sep, task #178 — Mexico as a one-click country. One instruction took five
messages ("…in both develop as well as release 3.1.6", "no do the change and
create PR", "approve 178", "push and raise PR", "approval") and ended with
nothing pushed. Everything that went wrong that afternoon is pinned here:

  * his go-ahead was read as feedback on a colleague's draft, and the chat brain
    answered twice that it had "no tool that can approve";
  * the plan he was asked to approve reached his phone as one line — the brain
    had fenced the whole plan, and fences are dropped;
  * the task could not do the second base branch, and held the push because his
    relayed words "arrived as an injected block";
  * Sankalp's "enable in uat and pp both" arrived while Claude was out, was read
    by the rules as nothing to check, and never reached the draft;
  * "CC: Abhijit…" became what Sankalp had "asked about";
  * Vinish's "share the whole text flow" was answered by quoting an earlier chat
    message that had since been corrected;
  * the budget warning came twice, with a reset time that moved every tick.

His rule, the same day: "for a big coding task, plan and get approval is fine,
but getting too much approval is drag."
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

import pytest

from app import answers, brains, go, main, store, tasks, understand

PLAN_178 = """Same values on develop and release/3.1.6, matching prod-values.yml exactly on both.

```
STRUCTURE
  helm/prod-values.yml:135   LIST_OF_ONE_CLICK_COUNTRIES value

FLOW
  develop branch          add MX to prod-values.yml list, commit
  release/3.1.6 branch    same edit, separate commit

RISK: none — single-line value edit, no code path

FLAGGED: the same key exists in every other env file too (uat, preprod, qa, sit,
dev, spt) — MX is absent from all of them, not just prod.

PLAN READY
```"""

BIG_PLAN = """STRUCTURE
  BookingService          validates the ETA
  EtaValidator            new, rejects late ETA
  ServicePlanMapper       carries the field
  BookingServiceTest      +3 cases

FLOW
  vessel feed     sends the ETA
  BookingService  validates it   <- change

RISK: medium

PLAN READY"""


class _Sink:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, payload):
        self.sent.append(payload)


def _conv():
    c = store.create_conversation(model="claude_cli", workspace=None)
    c["model"] = "claude_cli"
    return c


def _no_brain(*a, **k):
    raise AssertionError("a brain was asked to do what the task table already knows")


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    pushed: list[str] = []

    async def notify(msg, kind="task", **k):
        pushed.append(msg)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: str(tmp_path))
    monkeypatch.setattr(tasks, "committed_so_far", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "_audit_note", lambda tid: "")
    monkeypatch.setattr(tasks, "_learn_from", lambda *a, **k: None)
    monkeypatch.setenv("ASTA_PLAN_APPROVAL_FROM_TIER", "2")
    monkeypatch.setenv("ASTA_GO_AFTER_PLAN_SECONDS", "0")
    return pushed


# --- what counts as his go-ahead ---------------------------------------------

@pytest.mark.parametrize("said, pr", [
    ("no do the change and create PR", True),
    ("push and raise PR", True),
    ("push fast please now itself", True),
    ("raise PR 178", True),
    ("create pr", True),
    ("ship it", True),
    ("ok push it now", True),
    ("approval", False),
    ("Go ahead approve", False),
    ("go ahead approve and raise PR", True),
])
def test_every_way_he_says_go_is_a_command(said, pr):
    is_go, _n = go.command(said)
    assert is_go, said
    assert go.wants_pr(said) is pr


@pytest.mark.parametrize("said", [
    "go ahead", "do it", "send", "yes", "no", "don't push yet",
    "push the ETA fix after CI is green", "can you push it without claude",
    "approve it once CI is green", "what is the approval process for release",
])
def test_what_is_not_a_go_ahead_on_a_task(said):
    """"go ahead" and "do it" on their own answer whatever he was last asked — a
    draft, an offer. A sentence that merely contains "push" is a sentence."""
    assert go.command(said) == (False, None)


def test_a_brief_that_says_do_not_push_is_not_a_request_to_push():
    assert go.wants_pr("commit locally. Do not push or open a PR — stop after committing") is False
    assert go.wants_pr("enable MX in develop and 3.1.6") is False
    assert go.wants_pr("enable MX in develop and raise the PR") is True


# --- the five messages, replayed ----------------------------------------------

def test_do_the_change_and_create_pr_approves_the_waiting_plan_without_a_brain(monkeypatch, quiet):
    approved: list[int] = []

    async def approve(tid):
        approved.append(tid)
        return "ok"

    monkeypatch.setattr(tasks, "approve", approve)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    t = store.create_task("Enable MX as single-click country", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval", result=PLAN_178)
    sink = _Sink()
    assert asyncio.run(main._dispatch(conv, "no do the change and create PR", sink, "whatsapp")) is None
    assert approved == [t["id"]]
    assert go.ships(t["id"]), "and the PR is raised when it finishes — he already asked"
    assert "No more asks" in str(sink.sent)


def test_a_waiting_draft_does_not_swallow_his_go_ahead(monkeypatch, quiet):
    """A colleague's draft was waiting for "send". "push and raise PR" is not
    feedback on it."""
    from app import loop
    shipped: list[int] = []

    async def ship(tid):
        shipped.append(tid)
        return f"🔀 Task #{tid} shipped"

    monkeypatch.setattr(tasks, "ship", ship)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    loop.stage(conv["id"], {"type": "answer", "kind": "send", "what": "hello", "to": "Vinish Kumar",
                            "channel": "teams", "who": "Vinish Kumar", "_shown": time.time()})
    t = store.create_task("Enable MX as single-click country", "code", "p", None)
    store.update_task(t["id"], status="done", result="Implemented")
    sink = _Sink()
    assert asyncio.run(main._dispatch(conv, "push and raise PR", sink, "whatsapp")) is None
    assert shipped == [t["id"]]
    assert loop.awaiting(conv["id"]), "the draft is still waiting for its own answer"


def test_go_on_a_running_task_is_remembered_for_when_it_finishes(monkeypatch, quiet):
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    t = store.create_task("Enable MX", "code", "p", None)
    store.update_task(t["id"], status="running")
    sink = _Sink()
    asyncio.run(main._dispatch(conv, "push and raise PR", sink, "whatsapp"))
    assert go.ships(t["id"])
    assert "the moment it's done" in str(sink.sent)


def test_a_finished_task_he_said_go_on_is_shipped_not_asked_about(monkeypatch, quiet):
    shipped: list[int] = []

    async def ship(tid):
        shipped.append(tid)
        return "shipped"

    async def nothing(*a, **k):
        return ""

    monkeypatch.setattr(tasks, "ship", ship)
    monkeypatch.setattr(tasks, "_self_review", nothing)
    t = store.create_task("Enable MX", "code", "p", None)
    go.grant(t["id"], "do the change and create PR", ship=True)
    asyncio.run(tasks.complete(t["id"], t, "Added MX."))
    assert shipped == [t["id"]]
    assert not any("Say *raise PR*" in p for p in quiet)


def test_a_finished_task_he_did_not_say_go_on_stays_local(monkeypatch, quiet):
    async def ship(tid):
        raise AssertionError("pushed without being asked")

    async def nothing(*a, **k):
        return ""

    monkeypatch.setattr(tasks, "ship", ship)
    monkeypatch.setattr(tasks, "_self_review", nothing)
    t = store.create_task("Enable MX", "code", "p", None)
    asyncio.run(tasks.complete(t["id"], t, "Added MX."))
    assert any("nothing pushed" in p and "raise PR" in p for p in quiet)


def test_his_own_ask_saying_raise_the_pr_carries_the_go_ahead():
    t = store.create_task("Enable MX", "code", "p", None)
    assert go.on_spawn(t["id"], "enable MX as single click in develop and raise PR") is True
    assert go.ships(t["id"])
    t2 = store.create_task("Enable ES", "code", "p", None)
    assert go.on_spawn(t2["id"], "enable ES as single click in develop") is False
    assert go.granted(t2["id"]) is None


# --- which plans wait for him ---------------------------------------------------

def _announce(t, plan, monkeypatch):
    approved: list[int] = []

    async def approve(tid):
        approved.append(tid)
        return "ok"

    monkeypatch.setattr(tasks, "approve", approve)

    async def run():
        await tasks.announce_plan(t["id"], t, plan)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    return approved


def test_a_small_plan_is_shown_and_goes_ahead(monkeypatch, quiet):
    t = store.create_task("Enable MX", "code", "p", None)
    assert _announce(t, PLAN_178, monkeypatch) == [t["id"]]
    assert "going ahead" in quiet[0] and "approve task" not in quiet[0]
    assert f"stop {t['id']}" in quiet[0], "he can still stop it"
    assert "stays local until you say" in quiet[0], "and nothing is pushed unasked"


def test_a_big_plan_still_waits_for_him(monkeypatch, quiet):
    t = store.create_task("ETA validation", "code", "p", None)
    assert _announce(t, BIG_PLAN, monkeypatch) == []
    assert "approve task" in quiet[0]


def test_a_big_plan_he_already_said_go_on_does_not_wait(monkeypatch, quiet):
    t = store.create_task("ETA validation", "code", "p", None)
    go.grant(t["id"], "do it and raise the PR", ship=True)
    assert _announce(t, BIG_PLAN, monkeypatch) == [t["id"]]
    assert "as you said" in quiet[0] and "I'll raise the PR" in quiet[0]


def test_every_plan_can_be_made_to_wait_again(monkeypatch, quiet):
    monkeypatch.setenv("ASTA_PLAN_APPROVAL_FROM_TIER", "1")
    t = store.create_task("Enable MX", "code", "p", None)
    assert _announce(t, PLAN_178, monkeypatch) == []


def test_a_plan_stopped_meanwhile_is_not_started(monkeypatch, quiet):
    called: list[int] = []

    async def approve(tid):
        called.append(tid)

    monkeypatch.setattr(tasks, "approve", approve)
    t = store.create_task("Enable MX", "code", "p", None)
    store.update_task(t["id"], status="cancelled")
    asyncio.run(tasks._go_on(t["id"]))
    assert called == []


# --- the plan he reads ----------------------------------------------------------

def test_a_plan_wrapped_in_a_fence_still_reaches_his_phone():
    text = tasks._phone_text(PLAN_178, 1100)
    assert "helm/prod-values.yml" in text
    assert "develop branch" in text
    assert "RISK: none" in text
    assert "FLAGGED: the same key exists" in text, "the one line that would have caught the misread"
    assert "PLAN READY" not in text and "```" not in text


def test_an_ordinary_code_fence_is_still_dropped():
    text = tasks._phone_text("STRUCTURE\n  X  adds y\n\n```\nmvn -q test\nBUILD OK\n```\n\nRISK: low", 1100)
    assert "mvn" not in text and "RISK: low" in text


# --- his word to a task, and the second branch ----------------------------------

def test_his_follow_up_reaches_the_task_as_his_not_as_an_injected_block():
    t = store.create_task("Enable MX", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval")
    tasks.augment(t["id"], "add uat and preprod as well")
    text = tasks._drain_addenda(t["id"])
    assert "Arun said this himself" in text and "add uat and preprod as well" in text
    assert "do not report it as blocked" in text
    assert "[Additional instructions" not in text


def test_the_worker_is_told_the_second_branch_and_the_push_are_astas():
    assert "ONE BRANCH, ONE WORKTREE" in tasks.CODE_OVERRIDES
    assert "Asta ports the commit" in tasks.CODE_OVERRIDES


def test_both_branches_named_means_both_get_a_pr(monkeypatch, tmp_path):
    async def git(cwd, *args, **k):
        return (0, "abc") if "origin/release/3.1.6" in args else (1, "")

    monkeypatch.setattr(tasks.repo_ops, "git", git)
    t = {"title": "Enable MX", "prompt": "add MX in both develop as well as latest release "
                                         "branch 3.1.6 in booking service"}
    tid = store.create_task(t["title"], "code", t["prompt"], None)["id"]
    assert asyncio.run(tasks._other_bases(tid, t, tmp_path, "origin/develop")) == ["release/3.1.6"]
    only = {"title": "Fix", "prompt": "fix the NPE that came in with release 3.1.6"}
    assert asyncio.run(tasks._other_bases(tid, only, tmp_path, "origin/develop")) == [], \
        "a release merely mentioned is not a second target"


def test_porting_cherry_picks_onto_the_other_base_and_opens_its_pr(monkeypatch, tmp_path):
    ran: list[tuple] = []

    async def git(cwd, *args, **k):
        ran.append(args)
        if args[:2] == ("git", "rev-list"):
            return 0, "c1\nc2\n"
        if args[:3] == ("gh", "pr", "create"):
            return 0, "https://github.com/acme/svc/pull/77\n"
        return 0, ""

    monkeypatch.setattr(tasks.repo_ops, "git", git)
    url = asyncio.run(tasks._port(tmp_path, "feature/asta-1-mx", "origin/develop", "release/3.1.6"))
    assert url == "https://github.com/acme/svc/pull/77"
    assert ("git", "cherry-pick", "c1", "c2") in ran
    create = next(a for a in ran if a[:3] == ("gh", "pr", "create"))
    assert create[create.index("--base") + 1] == "release/3.1.6"
    assert any(a[:3] == ("git", "worktree", "remove") for a in ran), "the temp checkout is cleaned up"


def test_a_port_that_conflicts_says_so_and_does_not_push(monkeypatch, tmp_path):
    ran: list[tuple] = []

    async def git(cwd, *args, **k):
        ran.append(args)
        if args[:2] == ("git", "rev-list"):
            return 0, "c1\n"
        if args[:2] == ("git", "cherry-pick") and "--abort" not in args:
            return 1, "CONFLICT (content)"
        return 0, ""

    monkeypatch.setattr(tasks.repo_ops, "git", git)
    out = asyncio.run(tasks._port(tmp_path, "feature/x", "origin/develop", "release/3.1.6"))
    assert "does not apply cleanly" in out
    assert not any(a[:2] == ("git", "push") for a in ran)


# --- what a colleague actually asked, and what they added -----------------------

def test_a_cc_line_is_never_the_ask():
    d = understand.rules({"new": [
        "Sankalp Grover: Hi Vinish Kumar/Arunkumar K Can we please enable single click for "
        "Mexico from backend as well ? It is enabled from Ui now",
        "Sankalp Grover: CC: Abhijit Mohapatra Maramreddy Rajasekhar Reddy"]})
    assert d["state"] == "ask"
    assert d["need"].startswith("Hi Vinish Kumar/Arunkumar K Can we please enable single click")


def _answer_task(thread, who, prompt, status, age):
    t = store.create_task(f"{who} asked", "analysis", prompt, None)
    # An older task, not one made this second.
    store.update_task(t["id"], status=status, created_at=time.time() - age,
                      finished_at=time.time() - 60 if status == "done" else None)
    answers.remember_meta(t["id"], who=who, need="enable MX", chat=who, group=False, thread=thread)
    return t["id"]


def test_what_they_add_while_it_is_being_worked_reaches_him_with_the_draft(monkeypatch):
    shown: list[str] = []

    async def notify(msg, kind="", **k):
        shown.append(msg)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    store.kv_set("wa_conversation", _conv()["id"])
    tid = _answer_task("teams:Sankalp Grover", "Sankalp Grover",
                       "Can we enable single click for Mexico", "running", age=300)
    kept = asyncio.run(answers.note_followup("teams:Sankalp Grover", "Sankalp Grover",
                                             "enable in uat and pp both"))
    assert kept == tid
    asyncio.run(answers.present(who="Sankalp Grover", need="enable MX", chat="Sankalp Grover",
                                group=False, analysis="not enabled in prod", reply="Will do.",
                                task_id=tid, thread="teams:Sankalp Grover"))
    assert "enable in uat and pp both" in shown[-1]
    assert "check the reply covers it" in shown[-1]


def test_the_message_that_started_it_is_not_its_own_follow_up():
    _answer_task("teams:Sankalp Grover", "Sankalp Grover",
                 "Can we enable single click for Mexico from backend", "running", age=300)
    assert asyncio.run(answers.note_followup(
        "teams:Sankalp Grover", "Sankalp Grover",
        "Can we enable single click for Mexico from backend")) is None


def test_a_follow_up_after_the_draft_is_shown_is_said_once(monkeypatch):
    from app import loop
    shown: list[str] = []

    async def notify(msg, kind="", **k):
        shown.append(msg)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    cid = _conv()["id"]
    store.kv_set("wa_conversation", cid)
    tid = _answer_task("teams:Sankalp Grover", "Sankalp Grover", "enable Mexico", "done", age=900)
    loop.stage(cid, {"type": "answer", "kind": "send", "what": "x", "to": "Sankalp Grover",
                     "channel": "teams", "task_id": tid, "_shown": time.time()})
    for _ in range(2):
        asyncio.run(answers.note_followup("teams:Sankalp Grover", "Sankalp Grover",
                                          "enable in uat and pp both"))
    assert len(shown) == 1 and "after that draft was written" in shown[0]


def test_a_waiting_colleague_is_investigated_without_asking_him_first(monkeypatch):
    from app import offers, responder
    monkeypatch.setenv("ASTA_RESPOND", "1")
    monkeypatch.setenv("ASTA_ASK_BEFORE_NEW_GROUND", "0")
    monkeypatch.setattr(responder, "familiar", lambda g: (False, ""))
    monkeypatch.setattr(responder, "should_respond", lambda *a, **k: "")
    monkeypatch.setattr(responder, "what_it_asks", lambda t: "debug")
    spawned: list[str] = []

    def spawn(title, brief, kind, *a, **k):
        spawned.append(kind)
        return {"id": 9, "title": title}

    monkeypatch.setattr(tasks, "spawn", spawn)
    t = responder.respond("teams-chat", "Sankalp Grover", "can you check why the quote screen fails?",
                          reply_to="Sankalp Grover", thread="teams:Sankalp Grover")
    assert t and spawned == ["analysis"], "read-only, and no 'want me to look into it?'"
    assert offers.pending() is None


# --- old chat is context, not proof ---------------------------------------------

def test_the_brief_says_earlier_messages_are_not_evidence_and_carries_the_latest_finding():
    from app import responder
    t = store.create_task("Vinish Kumar asked", "analysis", "p", None)
    store.update_task(t["id"], status="done", finished_at=time.time() - 600,
                      result="ANALYSIS:\nIt was the price-update save on an invoiced job, "
                             "not an ETA change.\n\nREPLY:\nIt was the price update.")
    answers.remember_meta(t["id"], who="Vinish Kumar", need="INC9702338", chat="Vinish Kumar",
                          group=False, thread="teams:Vinish Kumar")
    brief = responder._waiting_brief("Vinish Kumar", "Can you share the whole text flow?",
                                     "Arun: the ETA update triggered the cancellation")
    assert "What was SAID in the chat is not what is TRUE" in brief
    assert "price-update save" in brief and f"task #{t['id']}" in brief


# --- a label is not a message -----------------------------------------------------

def test_a_description_of_a_message_is_not_staged_as_the_message(monkeypatch):
    from app import agent
    monkeypatch.setattr(tasks, "current_conversation", lambda: _conv()["id"])
    out = agent.prepare_to_send("Reply confirming Mexico single-click backend status",
                                to="Sankalp Grover", channel="teams")
    assert out.startswith("Not staged"), out


# --- the budget window -------------------------------------------------------------

def test_the_window_does_not_slide_with_the_clock():
    """Continuous use, no stated reset: the chain used to start from whatever
    was oldest inside the lookback, so the reset moved forward every tick."""
    t0 = dt.datetime(2026, 9, 30, 4, 0).timestamp()
    evs = [(t0 + i * 600, 1.0, False) for i in range(80)]          # every 10 min, 13 hours
    first = brains.window_start(t0 + 11 * 3600, evs)
    later = brains.window_start(t0 + 11 * 3600 + 900, evs)
    assert first == later, "the same window fifteen minutes on"


def test_a_past_reset_is_where_the_next_window_is_counted_from():
    reset = dt.datetime(2026, 9, 30, 15, 0).timestamp()
    store.kv_set("claude_resets", json.dumps([reset]))
    evs = [(reset - 3 * 3600 + i * 300, 1.0, False) for i in range(60)]   # 12:00 → 17:00
    start = brains.window_start(reset + 8 * 60, evs)
    assert reset <= start <= reset + 5 * 60, "the first use after 3:00pm, not before it"
    assert brains.window_start(reset + 40 * 60, evs) == start


def test_the_warning_reads_as_a_sentence(monkeypatch, tmp_path):
    said: list[str] = []

    async def notify(msg, kind="", **k):
        said.append(msg)

    monkeypatch.setattr(brains, "claude_status", lambda now: {
        "used": 9.9e6, "asta": 0.8e6, "yours": 9.1e6, "ceiling": 12e6, "share": 0.82,
        "resets_at": dt.datetime(2026, 9, 30, 15, 40).timestamp(), "limited": False,
        "measured_hits": 2})
    monkeypatch.setattr(brains, "copilot_status", lambda now: {"out": True, "resets_on": "1 Oct"})
    for _ in range(3):
        asyncio.run(brains.tick(notify, now=dt.datetime(2026, 9, 30, 14, 0).timestamp()))
    assert len(said) == 1, "once per window"
    assert "which resets 3:40pm" in said[0]
    assert "so it stays on Claude." in said[0] and "— or Copilot" not in said[0]


# --- a ping is not a question about the old conversation ------------------------

def test_bro_is_an_opener_with_no_guess_about_the_old_conversation():
    """Vinish, 30 Sep 16:00: "Bro" got "Sure — is this about equipment container
    MNBU0654520 dual-state discrepancy, or something else?" His words: "don't
    respond blindly — if he said bro, you are answering the old conversation."""
    d = understand.settle(
        {"source": "model", "state": "ask", "subject": "continuing",
         "need": "wants Arun to confirm logs match his finding",
         "guess": "equipment container MNBU0654520 dual-state discrepancy",
         "question": "", "reply": "On it"},
        {"new": ["Bro"]})
    assert d["state"] == "opener" and d["question"] == "" and d["guess"] == ""
    assert d["reply"] == ""


@pytest.mark.parametrize("said, ping", [
    (["Bro"], True), (["Hi Arun"], True), (["hello"], True), (["Arun?"], True), (["??"], True),
    (["Call ?"], False), (["free?"], False), (["Bro check H65ZMWX52B2"], False),
    (["Bro", "can you check the PR"], False),
])
def test_what_a_ping_is(said, ping):
    assert understand.is_ping(said) is ping


def test_a_question_about_the_person_is_not_sent_to_that_person():
    q = ("Do your logs match what Vinish found—that MNBU0654520 was in both "
         "bookingEquipments and cancelledBookingEquipments on 5 Sep?")
    assert understand.safe_question(q, "Vinish Kumar") == ""
    assert understand.safe_question("Which booking is this about?", "Vinish Kumar") \
        == "Which booking is this about?"


def test_the_ping_answer_uses_the_term_he_uses_for_that_person(monkeypatch):
    from app import steward, writing
    monkeypatch.setattr(writing, "address_terms", lambda chat, limit=400: ["bro"])
    assert steward.ping_back("Vinish Kumar") == "Yes bro, tell me"
    monkeypatch.setattr(writing, "address_terms", lambda chat, limit=400: [])
    assert steward.ping_back("Komal Jayswal") == "Yes, tell me"


def test_a_named_container_or_pr_is_checked_not_asked_about():
    from app import chat_watch
    assert chat_watch._concrete("It appears in both fields for container MNBU0654520 on 5 Sep")
    assert chat_watch._concrete("please review https://github.com/x/y/pull/1459")
    assert not chat_watch._concrete("can we talk about the design?")


def _nudge(monkeypatch, *, staged=False, task=None):
    from app import chat_watch, loop, responder
    asked: list[dict] = []

    def respond(source, who, text, **kw):
        asked.append({"text": text, **kw})
        return task

    monkeypatch.setattr(responder, "respond", respond)
    cid = _conv()["id"]
    store.kv_set("wa_conversation", cid)
    if staged:
        loop.stage(cid, {"type": "answer", "kind": "send", "what": "x", "to": "Vinish Kumar",
                         "channel": "teams", "thread": "teams:Vinish Kumar", "_shown": time.time()})
    c = {"open_need": "confirm the logs match his finding for MNBU0654520", "chat": "Vinish Kumar",
         "keys": ["k1"], "sent_at": None, "so_far": "Vinish found the container in both fields",
         "conversation": ["Vinish Kumar: It appears in both fields", "Vinish Kumar: Bro"]}
    line = asyncio.run(chat_watch._nudged("teams:Vinish Kumar", c, "Vinish Kumar", time.time()))
    return line, asked


def test_a_ping_while_they_are_waiting_is_worked_and_he_is_told_once(monkeypatch):
    line, asked = _nudge(monkeypatch, task={"id": 41, "title": "t"})
    assert "pinged again" in line and "MNBU0654520" in line and "task #41" in line
    assert asked and asked[0]["text"].startswith("confirm the logs match")
    assert asked[0]["questions"] == ["confirm the logs match his finding for MNBU0654520"]
    again, _ = _nudge(monkeypatch, task={"id": 41, "title": "t"})
    assert again == "", "the same line is not sent twice"


def test_a_ping_with_nothing_checkable_says_they_are_waiting_on_him(monkeypatch):
    line, _ = _nudge(monkeypatch, task=None)
    assert "still waiting on you for" in line


def test_a_ping_while_his_draft_is_waiting_points_at_the_draft(monkeypatch):
    line, asked = _nudge(monkeypatch, staged=True)
    assert "waiting on your *send*" in line and asked == []


def test_rejecting_a_task_lets_go_of_its_clean_checkout(monkeypatch, tmp_path):
    """#179 rejected, #180 started to replace it: "could not create a worktree —
    already checked out at …task-179"."""
    from app import worktrees
    removed: list[int] = []

    async def cancel(tid, status="cancelled", why=""):
        return False

    async def remove(root, tid, force=False):
        removed.append(tid)
        return []

    monkeypatch.setattr(tasks, "cancel", cancel)
    monkeypatch.setattr(tasks, "learn_from_stop", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "code_cwd", lambda ws: str(tmp_path))
    monkeypatch.setattr(worktrees, "exists", lambda root, tid: True)
    monkeypatch.setattr(worktrees, "remove", remove)
    t = store.create_task("x", "code", "p", None)
    asyncio.run(tasks.reject(t["id"], "wrong place"))
    assert removed == [t["id"]]


# --- after the PR is raised: he is told, and Asta can act on it --------------------

def test_his_own_work_is_never_held_back_by_the_daily_budget(monkeypatch):
    """The day's twenty pushes were spent; the plan, DONE, the PR and "CI red"
    for #180 all went to the digest. "Until I come and ask, no update."""
    from app import attention, budget
    monkeypatch.setenv("ASTA_PUSH_BUDGET", "20")
    monkeypatch.setattr(budget, "spent", lambda now=None: 23)
    for level in ("task", "action", "ci", "answer", "calls"):
        assert budget.allows(attention.P_TODAY, "direct", level=level), level
    assert not budget.allows(attention.P_FYI, "ambient", level="jira")


def test_a_push_build_and_a_pull_request_build_both_count():
    """Two "cicd / Build" checks on one commit — the push trigger's failed, the
    pull_request trigger's passed. "CI is green now, the fail is stale" was wrong."""
    pr = {"statusCheckRollup": [
        {"workflowName": "cicd", "name": "Build", "conclusion": "FAILURE", "startedAt": "1",
         "detailsUrl": "https://github.com/acme/svc/actions/runs/36709097728/job/1"},
        {"workflowName": "cicd", "name": "Build", "conclusion": "SUCCESS", "startedAt": "2",
         "detailsUrl": "https://github.com/acme/svc/actions/runs/36709111717/job/2"}]}
    assert tasks._checks_verdict(pr) == "red"
    assert tasks._failed_runs(pr) == ["36709097728"]


def _shipped(monkeypatch, rollup, log=""):
    t = store.create_task("Add derived ATA/ATD", "code", "p", None)
    store.update_task(t["id"], status="shipped", pr_state="OPEN",
                      pr_urls="svc: https://github.com/acme/svc/pull/1252")
    ran: list[tuple] = []

    async def state(url):
        return {"state": "OPEN", "statusCheckRollup": rollup, "reviewDecision": ""}

    async def git(cwd, *args, **k):
        ran.append((args, k.get("stdin", "")))
        if args[:3] == ("gh", "run", "view"):
            return 0, log
        if args[:3] == ("gh", "pr", "view"):
            edits = [a[1] for a in ran if a[0][:3] == ("gh", "pr", "edit")]
            return 0, edits[-1] if edits else "Existing description\n"
        return 0, ""

    monkeypatch.setattr(tasks, "_pr_state", state)
    monkeypatch.setattr(tasks.repo_ops, "git", git)
    return t["id"], ran


def test_ci_red_names_the_failing_test_and_offers_the_rerun(monkeypatch):
    rollup = [{"workflowName": "cicd", "name": "Build", "conclusion": "FAILURE",
               "startedAt": "1", "detailsUrl": "https://github.com/acme/svc/actions/runs/777/job/1"}]
    log = ("build\tRun tests\t[ERROR] ReadyForPlanningActivityImplTest."
           "readyForPlanning_BookingRfpFailedStatus_ClosesActivityAsFailed:212 -- expected: <A> but was: <B>\n")
    monkeypatch.setenv("ASTA_CI_AUTO_RERUN", "1")
    tid, ran = _shipped(monkeypatch, rollup, log)
    line = asyncio.run(tasks.check_pr(tid))
    assert "CI red" in line and "ReadyForPlanningActivityImplTest" in line
    assert "Re-running the failed jobs once" in line, "the first red is re-run on its own"
    assert any(a[0][:4] == ("gh", "run", "rerun", "777") for a in ran)
    store.update_task(tid, pr_state="OPEN")
    again = asyncio.run(tasks.check_pr(tid))
    assert f"rerun ci {tid}" in again and f"fix #{tid}" in again, "a second red is his call"
    assert sum(1 for a in ran if a[0][:3] == ("gh", "run", "rerun")) == 1


def test_ci_turning_green_is_news_too(monkeypatch):
    rollup = [{"workflowName": "cicd", "name": "Build", "conclusion": "SUCCESS", "startedAt": "1"}]
    tid, _ = _shipped(monkeypatch, rollup)
    line = asyncio.run(tasks.check_pr(tid))
    assert "CI green" in line and "Waiting on review" in line
    assert asyncio.run(tasks.check_pr(tid)) is None, "said once"


def test_rerun_it_reruns_the_failed_jobs_without_a_brain(monkeypatch):
    rollup = [{"workflowName": "cicd", "name": "Build", "conclusion": "FAILURE",
               "startedAt": "1", "detailsUrl": "https://github.com/acme/svc/actions/runs/777/job/1"}]
    tid, ran = _shipped(monkeypatch, rollup)
    store.update_task(tid, status="pr_ci_failed")
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    sink = _Sink()
    assert asyncio.run(main._dispatch(_conv(), "rerun it", sink, "whatsapp")) is None
    assert any(a[0][:4] == ("gh", "run", "rerun", "777") and "--failed" in a[0] for a in ran)
    assert "re-running the failed jobs" in str(sink.sent)


def test_a_note_is_added_to_his_pr_description_and_checked(monkeypatch):
    tid, ran = _shipped(monkeypatch, [])
    out = asyncio.run(tasks.pr_note(tid, "Known follow-up: ATD is not in the whitelist yet."))
    assert out.startswith("📝 Added to the description")
    edit = next(a for a in ran if a[0][:3] == ("gh", "pr", "edit"))
    assert edit[1].startswith("Existing description") and "Known follow-up: ATD" in edit[1]


def test_an_approved_pr_draft_is_a_recorded_call_not_a_prompt(monkeypatch):
    """"send" on the staged PR note went back to a brain that cannot run gh."""
    tid, _ = _shipped(monkeypatch, [])
    op = main._mechanical_send({
        "channel": "pr", "to": "PR #1252 — svc",
        "what": "Add to PR #1252 description (svc):\n\n**Known follow-up:** ATD is not whitelisted."})
    assert op == {"name": "pr_note", "args": {"task_id": tid, "where": "description",
                                              "text": "**Known follow-up:** ATD is not whitelisted."}}


def test_a_question_about_finished_work_does_not_reopen_it(monkeypatch):
    """"task 180 done ? how long it will take ?" was applied to #180 as feedback."""
    from app import frontdesk
    assert frontdesk.is_question("task 180 done ? how long it will take ?")
    assert frontdesk.is_question("is it pushed")
    assert not frontdesk.is_question("180 also cover the amend path")
    assert not frontdesk.is_question("can you also add a null check?")

    async def refine(tid, text):
        raise AssertionError("a question reopened the task")

    monkeypatch.setattr(tasks, "refine", refine)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    t = store.create_task("Add derived ATA/ATD", "code", "p", None)
    store.update_task(t["id"], status="done", result="Implemented")
    tasks.link_task(conv["id"], t["id"])
    sink = _Sink()
    asyncio.run(main._dispatch(conv, f"task {t['id']} done ? how long it will take ?", sink, "whatsapp"))
    assert f"#{t['id']}" in str(sink.sent) and "done" in str(sink.sent)


# --- Teams: what could not be read, and what is open -------------------------------

def test_an_unnamed_group_chat_is_opened_by_its_own_rail_row():
    from app import teams_bridge as tb
    assert tb._is_member_list("Shabda Anubhav, Vinish, +2")
    assert not tb._is_member_list("Rekha and Rini"), "search first; the row only if search fails"
    assert tb._maybe_member_list("Rekha and Rini")
    assert not tb._is_member_list("Team Booking and Execution")
    assert not tb._is_member_list("General")
    assert not tb._is_member_list("Backend Community of Practice")
    assert tb._rail_title_ok("Shabda Anubhav Dev, Vinish Kumar, Yogesh Kumar Ravichandran",
                             "Shabda Anubhav, Vinish, +2")
    assert not tb._rail_title_ok("Daily deployment slot", "Shabda Anubhav, Vinish, +2")


def test_any_message_for_me_lists_what_is_open_and_what_could_not_be_read():
    from app import chat_watch, threads
    t = threads.open("teams", "Fake Internal Team", chat="Fake Internal Team")
    threads.update(t["id"], status="awaiting_arun", need="Review PR #1459",
                   last_activity=time.time())
    store.kv_set("chatwatch_rail", json.dumps(["Shabda Anubhav, Vinish, +2", "Vinish Kumar"]))
    chat_watch.note_unopenable("Shabda Anubhav, Vinish, +2")
    lines = chat_watch.open_with_him()
    assert any("Fake Internal Team" in ln and "waiting on you: Review PR #1459" in ln for ln in lines)
    assert any("could not open" in ln and "Shabda Anubhav, Vinish, +2" in ln for ln in lines)


# --- a send he asked for is sent ---------------------------------------------------

def _asked(monkeypatch, said, earlier=()):
    from app import agent, capabilities
    cid = _conv()["id"]
    for e in earlier:
        store.add_ui_message(cid, "user", e, {})
    monkeypatch.setenv("ASTA_SEND_WHEN_ASKED", "1")
    monkeypatch.setattr(capabilities, "said_this_turn", lambda: said)
    return agent, cid


def test_a_message_he_asked_for_by_name_is_not_staged_back_to_him(monkeypatch):
    agent, cid = _asked(monkeypatch, "share this PR with vinish and ask him to review")
    assert agent._he_asked_to_send("Vinish Kumar", cid)
    assert not agent._he_asked_to_send("Komal Jayswal", cid), "someone he did not name"


def test_him_means_the_person_he_named_a_moment_ago(monkeypatch):
    agent, cid = _asked(monkeypatch, "share this PR with him ask him to review",
                        earlier=["what abt the vinish msg to vinish did you send or not ?"])
    assert agent._he_asked_to_send("Vinish Kumar", cid)


def test_a_turn_that_never_asked_for_a_send_still_stages(monkeypatch):
    agent, cid = _asked(monkeypatch, "what did vinish say about the RCA?")
    assert not agent._he_asked_to_send("Vinish Kumar", cid)


def test_the_send_he_asked_for_goes_out_as_a_recorded_call(monkeypatch):
    from app import loop, ops
    sent: list[dict] = []

    async def run(op):
        sent.append(op)
        return "📨 Sent to Vinish Kumar."

    async def notify(msg, kind="", **k):
        pass

    from app import notify as notify_mod
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(notify_mod, "notify", notify)
    agent, cid = _asked(monkeypatch, "share this PR with vinish, ask him to review")
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)

    async def go_():
        out = agent.prepare_to_send("can u review this PR when free\nhttps://github.com/a/b/pull/1252",
                                    to="Vinish Kumar", channel="teams")
        await asyncio.sleep(0.05)
        return out

    out = asyncio.run(go_())
    assert out.startswith("Sending to Vinish Kumar now")
    assert sent and sent[0]["name"] == "teams_send" and sent[0]["args"]["to_group"] is False
    assert not loop.awaiting(cid), "nothing is left waiting for a second yes"


def test_a_group_is_never_sent_without_his_yes(monkeypatch):
    from app import ops

    async def run(op):
        raise AssertionError("sent to a group without his yes")

    monkeypatch.setattr(ops, "run", run)
    agent, cid = _asked(monkeypatch, "share this PR with the defect triage group")
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)

    async def go_():
        out = agent.prepare_to_send("please review", to="Defect Triage", channel="teams",
                                    to_group=True)
        await asyncio.sleep(0.05)
        return out

    assert not asyncio.run(go_()).startswith("Sending to")


def test_a_pr_marked_red_with_green_checks_is_reported_as_recovered(monkeypatch):
    """The state string already said green; the task still said CI failed."""
    rollup = [{"workflowName": "cicd", "name": "Build", "conclusion": "SUCCESS", "startedAt": "1"}]
    tid, _ = _shipped(monkeypatch, rollup)
    store.update_task(tid, status="pr_ci_failed", pr_state="green/NONE")
    line = asyncio.run(tasks.check_pr(tid))
    assert line and "CI green" in line
    assert store.get_task(tid)["status"] == "shipped"
    assert asyncio.run(tasks.check_pr(tid)) is None


# --- task #185: an existing PR branch, and an honest DONE -------------------------

def test_a_task_is_told_it_may_switch_to_an_existing_branch_without_asking():
    assert "EXISTING branch or PR" in tasks.CODE_OVERRIDES
    assert "not a" in tasks.CODE_OVERRIDES and "question to bring back to him" in tasks.CODE_OVERRIDES
    from pathlib import Path
    for f in ("agents/solo.md", "agents/micro.md"):
        assert "EXISTING branch or PR" in Path(f).read_text()


def test_the_self_review_reads_only_what_this_task_committed(monkeypatch, tmp_path):
    """#185's five-line fix was reviewed as 126 files across three repos."""
    asked: list[tuple] = []

    async def git(cwd, *args, **k):
        asked.append(args)
        if args[:2] == ("git", "log"):
            return (0, "bbb\naaa\n") if cwd.name == "svc" else (0, "")
        if args[:2] == ("git", "rev-parse"):
            return 0, "parent000\n"
        return 0, ""

    monkeypatch.setattr(tasks.repo_ops, "git", git)
    t = {"created_at": 1790000000.0}
    assert asyncio.run(tasks._task_base(tmp_path / "svc", t)) == "parent000"
    assert asyncio.run(tasks._task_base(tmp_path / "other", t)) == "", "untouched repo: nothing to review"
    log = next(a for a in asked if a[:2] == ("git", "log"))
    assert "--since=@1789999999" in log
    assert ("git", "rev-parse", "--verify", "aaa~1") in asked, "the parent of its FIRST commit"


def test_work_already_on_origin_is_not_reported_as_local_only(monkeypatch, quiet, tmp_path):
    async def nothing(*a, **k):
        return ""

    async def pushed(tid, t):
        return ["telikos-booking-service: https://github.com/acme/svc/pull/1429"]

    monkeypatch.setattr(tasks, "_self_review", nothing)
    monkeypatch.setattr(tasks, "_already_pushed", pushed)
    t = store.create_task("Fix RFP validation messages", "code", "p", None)
    asyncio.run(tasks.complete(t["id"], t, "Pushed. Commit b57e396 on feature/rfp-mandatory-field-validation."))
    assert "Already pushed — the PR is updated" in quiet[-1] and "pull/1429" in quiet[-1]
    assert "nothing pushed" not in quiet[-1]
    assert store.get_task(t["id"])["status"] == "shipped", "and its CI is watched from here"


def test_waiting_on_a_task_is_not_a_step_to_run():
    assert main._WAITS_ON_A_TASK.search("Check task #185 status again; when it finishes, present the diff")
    assert main._WAITS_ON_A_TASK.search("Wait for task #185's own completion notification")
    assert not main._WAITS_ON_A_TASK.search("add the null check and run the mapper tests")


# --- reading Teams: every change, once -------------------------------------------

def _rail(*rows):
    return [{"name": n, "text": f"{n}\n{t}\n9:48 PM"} for n, t in rows]


def test_a_burst_reads_every_changed_chat_one_to_one_first():
    """A 1:1 arrives, then five group messages push it to sixth on the rail. It
    never "rose", missed the cap and waited for the rotation."""
    from app import chat_watch
    names = ["G1", "G2", "G3", "G4", "G5", "Vinish Kumar", "Quiet Group"]
    store.save_teams_messages([{"chat": "Vinish Kumar", "sender": "Vinish Kumar", "text": "hi",
                                "sent_at": time.time() - 3600, "key": "k0", "stamp": ""}])
    chat_watch.changed_first(_rail(*[(n, "old") for n in names]), names)      # first look
    after = _rail(("G1", "new 1"), ("G2", "new 2"), ("G3", "Arunkumar can you check"),
                  ("G4", "new 4"), ("G5", "new 5"), ("Vinish Kumar", "Bro"), ("Quiet Group", "old"))
    first = chat_watch.changed_first(after, names)
    assert "Quiet Group" not in first, "nothing changed there"
    assert set(first) == {"G1", "G2", "G3", "G4", "G5", "Vinish Kumar"}
    assert first[0] == "Vinish Kumar", "the 1:1 first, though it sits sixth on the rail"
    assert first.index("G3") < first.index("G1"), "a group that names him before one that does not"


def test_his_own_reply_and_the_clock_are_not_news():
    from app import chat_watch
    names = ["Sankalp Grover", "Team X"]
    chat_watch.changed_first(_rail(("Sankalp Grover", "okay sure"), ("Team X", "deploy done")), names)
    later = [{"name": "Sankalp Grover", "text": "Sankalp Grover\nYou: done, thanks\n9:50 PM"},
             {"name": "Team X", "text": "Team X\ndeploy done\nYesterday"}]
    assert chat_watch.changed_first(later, names) == [], \
        "his own reply means he dealt with it; 9:48 PM → Yesterday is the clock"


def test_what_does_not_fit_this_sweep_is_owed_to_the_next():
    from app import chat_watch
    store.kv_set(chat_watch._OWED_KEY, json.dumps(["Vinish Kumar"]))
    store.kv_set(chat_watch._PREVIEWS_KEY, json.dumps({"Vinish Kumar": "Bro"}))
    first = chat_watch.changed_first(_rail(("Vinish Kumar", "Bro")), ["Vinish Kumar"])
    assert first == ["Vinish Kumar"], "carried over even though nothing changed since"


def test_a_lost_mark_does_not_replay_the_thread():
    """Team Booking's old messages came back every few minutes all evening."""
    from app import chat_watch
    now = time.time()
    rows = [{"key": "a1", "sent_at": now - 7200, "text": "Hi Everyone"},
            {"key": "a2", "sent_at": now - 7100, "text": "Can we connect please\n\n1 Like reaction."}]
    chat_watch.remember("Team Booking and Execution", rows)
    rekeyed = [{"key": "b1", "sent_at": now - 7200, "text": "Hi Everyone"},
               {"key": "b2", "sent_at": now - 7100, "text": "Can we connect please\n\n2 Like reactions."}]
    assert chat_watch.unseen("Team Booking and Execution", rekeyed) == []
    newer = rekeyed + [{"key": "b3", "sent_at": now - 60, "text": "any update?"}]
    assert [r["key"] for r in chat_watch.unseen("Team Booking and Execution", newer)] == ["b3"]


def test_the_same_message_with_new_reactions_is_the_same_message():
    from app import chat_watch
    a = {"sender": "Vinish Kumar", "sent_at": 1790770000.0, "text": "Can we connect please"}
    b = {"sender": "Vinish Kumar", "sent_at": 1790770010.0,
         "text": "Can we connect please\n\n1 Laugh reaction."}
    assert chat_watch._identity(a) == chat_watch._identity(b)
    c = {"sender": "Vinish Kumar", "sent_at": 1790773600.0, "text": "Can we connect please"}
    assert chat_watch._identity(a) != chat_watch._identity(c), "said again an hour later is new"


def test_the_same_answer_is_not_put_to_him_twice_in_a_day(monkeypatch):
    shown: list[str] = []

    async def notify(msg, kind="", **k):
        shown.append(msg)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    store.kv_set("wa_conversation", _conv()["id"])
    kw = dict(who="Vinish Kumar", need="wants to connect", chat="Vinish Kumar", group=False,
              analysis="", reply="Sure bro, what time works for you?")
    assert asyncio.run(answers.present(**kw)) is True
    from app import loop
    loop.clear_awaiting(answers.phone_conversation())
    assert asyncio.run(answers.present(**kw)) is False
    assert len(shown) == 1


# --- calls: the moment Teams shows the toast ---------------------------------------

def test_a_ring_seen_by_the_page_is_offered_once_whichever_watcher_sees_it(monkeypatch):
    from app import incoming
    told: list[str] = []

    async def notify(msg, kind="", **k):
        told.append(kind)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    monkeypatch.setenv("ASTA_INCOMING", "1")
    monkeypatch.setenv("ASTA_RING_PUSH", "1")
    monkeypatch.setattr(incoming, "meetings_busy", lambda: False)
    incoming.clear()
    toast = "Vinish Kumar is calling you\nAccept\nDecline"
    assert asyncio.run(incoming.on_ring_text(toast)) is True
    assert asyncio.run(incoming.on_ring_text(toast)) is False, "the same ring, not asked twice"
    assert asyncio.run(incoming.offer({"who": "Vinish Kumar", "group": False})) is False, \
        "and the eight-second poll does not ask again either"
    assert told == ["calls"]


def test_a_page_with_no_call_in_it_says_nothing(monkeypatch):
    from app import incoming
    monkeypatch.setenv("ASTA_INCOMING", "1")
    assert asyncio.run(incoming.on_ring_text("Chat\nVinish Kumar\nBro")) is False
    assert "calling you" in incoming.RING_WATCH_JS and "astaRing" in incoming.RING_WATCH_JS


@pytest.mark.parametrize("previous, current, want", [
    # the 1:1 arrived first, then five groups: it sits sixth and never rose
    (["A", "B", "Vinish", "C", "D", "E", "F", "G", "H"],
     ["G", "F", "E", "D", "C", "Vinish", "A", "B", "H"], {"G", "F", "E", "D", "C", "Vinish"}),
    # nothing moved: only the head (a second message in the top chat moves nothing)
    (["A", "B", "C"], ["A", "B", "C"], {"A"}),
    # one message in B
    (["A", "B", "C", "D"], ["B", "A", "C", "D"], {"B"}),
    # a brand-new chat appears at the top
    (["A", "B"], ["New", "A", "B"], {"New"}),
])
def test_touched_reads_activity_from_order_alone(previous, current, want):
    from app import chat_watch
    assert set(chat_watch.touched(previous, current)) == want


def test_a_compact_rail_still_reads_the_burst_one_to_one_first():
    from app import chat_watch
    store.save_teams_messages([{"chat": "Vinish Kumar", "sender": "Vinish Kumar", "text": "hi",
                                "sent_at": time.time() - 3600, "key": "k1", "stamp": ""}])
    previous = ["A", "B", "Vinish Kumar", "C", "D", "E", "F", "G"]
    current = ["G", "F", "E", "D", "C", "Vinish Kumar", "A", "B"]
    rail = [{"name": n, "text": n} for n in current]           # names only, as live
    first = chat_watch.changed_first(rail, current, previous)
    assert first[0] == "Vinish Kumar" and set(first) == {"G", "F", "E", "D", "C", "Vinish Kumar"}
