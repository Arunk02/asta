"""The eight things wrong on 2026-09-07, each held by the evidence that found it.

None of these were guesses. Every number in here came out of his own database or
his own screenshots, and each test fails if the thing goes back to how it was.
"""

from __future__ import annotations

import asyncio

import pytest

from app import attention, chat_watch, incoming, outlook, responder, store, tasks


# --- 1. a task run had no tools at all -------------------------------------------
#
# Task #86: "no Jira MCP/API tool is connected in this session... no Teams tool
# available... prepare_to_send not among my available tools". Task #88 wrote the
# transportAssetPriority mapping across eleven files, build green, and finished
# with "I can't message Alex directly". The work done and nobody told.

def test_a_task_run_gets_astas_own_tools(monkeypatch):
    monkeypatch.setenv("ASTA_CLI_MCP", "1")
    monkeypatch.setenv("ASTA_DEV_MCP", "")            # the dev servers stay off
    cfg = tasks.task_tools(1, "/tmp/ws")
    assert cfg, "a task run with no MCP config is the bug this closes"
    assert "asta" in cfg.lower()


def test_the_dev_servers_no_longer_take_astas_with_them(monkeypatch):
    """`config_for` returned None whenever the dev servers were absent, which
    threw the base config away with them — and they are off by default."""
    from app import dev_mcp
    monkeypatch.setenv("ASTA_DEV_MCP", "")
    base = {"mcpServers": {"asta": {"command": "python"}}}
    assert dev_mcp.config_for("/tmp/ws", base) == base


def test_with_no_base_and_no_dev_servers_nothing_is_attached(monkeypatch):
    """The old no-op contract, unchanged: the command must not grow a flag."""
    from app import dev_mcp
    monkeypatch.setenv("ASTA_DEV_MCP", "")
    assert dev_mcp.config_json("/tmp/ws") == ""


def test_mcp_off_means_no_config(monkeypatch):
    monkeypatch.setenv("ASTA_CLI_MCP", "0")
    monkeypatch.setenv("ASTA_DEV_MCP", "")
    assert tasks.task_tools(1, "/tmp/ws") == ""


def test_a_task_knows_which_chat_to_answer_in():
    """`prepare_to_send` stages into a conversation. A task with none staged into
    nothing, which is the failure that made it useless over MCP before."""
    conv = store.create_conversation(model="copilot", workspace=None)
    tasks.link_task(conv["id"], 42)
    assert tasks.conversation_of(42) == conv["id"]


def test_an_unknown_task_does_not_invent_a_conversation():
    assert tasks.conversation_of(999999) == ""


# --- 2. "still waiting on you" for things he had answered ---------------------------
#
# 1,779 rows in `notified` against 90 `acted`. `mark_acted` existed and its only
# caller in the whole product was the bench.

def _owed(who: str, what: str, key: str, priority: int = 1) -> str:
    attention.consider("teams-chat", key, who=who, what=what, priority=priority)
    return key


def test_answering_someone_settles_what_they_were_owed(monkeypatch):
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    _owed("Alex Kumar", "Retry fix merged?", "k1")
    _owed("Alex Kumar", "Schema mapping pending?", "k2")
    _owed("Meera", "can you share the bug id", "k3")

    assert attention.settle_with("Alex Kumar") == 2
    assert store.attention_get("k1")["state"] == "acted"
    assert store.attention_get("k2")["state"] == "acted"
    # Somebody else's question is still open — settling is per person, not global.
    assert store.attention_get("k3")["state"] != "acted"


def test_one_reply_settles_the_whole_conversation(monkeypatch):
    """He asked three things in four minutes; one "yes" covers them. Settling only
    the exact key would leave the other two chasing him at end of day."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    for i in range(3):
        _owed("Alex Kumar", f"question {i}", f"q{i}")
    assert attention.settle_with("Alex Kumar") == 3


def test_astas_own_notices_are_never_settled_as_his_replies(monkeypatch):
    """"✅ DONE — #88" is Asta talking. Counting it as him answering someone
    would make the settle count a lie."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    attention.consider(attention.SELF_SOURCE, "self1", who="",
                       what="✅ DONE — #88 mapping", priority=1)
    assert attention.settle_with("") == 0


def test_sending_a_reply_clears_what_it_answered(monkeypatch):
    """Asta sent the reply itself and then chased him about the question it had
    just answered — "Retry fix merged?" was still owed after the send went out."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    _owed("Alex Kumar", "Retry fix merged?", "sent1")
    sent = {}

    async def fake_send(to, text, allow_group=False):
        sent["to"] = to
        return "Alex Kumar"

    from app import ops, teams_bridge
    monkeypatch.setattr(teams_bridge, "send_message", fake_send)
    out = asyncio.run(ops.run({"name": "teams_send",
                               "args": {"to": "Alex", "text": "retry fix is merged"}}))
    assert "Sent to Alex Kumar" in out
    assert store.attention_get("sent1")["state"] == "acted"


def test_a_message_that_is_not_his_can_never_be_chased(monkeypatch):
    """`consider` stamps a row `notified` the moment it decides to push. The sweep
    then applies a SECOND gate — is this actually his? — that the hourly chase
    knows nothing about. So a group conversation between other people was
    recorded as notified, withheld from his phone, and then chased at end of day.
    """
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    monkeypatch.setenv("ASTA_CHATWATCH", "1")

    async def one_group_chat():
        return ["release triage"]

    async def one_message(chat, advance=True):
        return [{"sender": "Anita Rao", "sent_at": 1_800_000_000.0,
                 "text": "Kavya Nair - we can have this done by Friday eod"}]

    monkeypatch.setattr(chat_watch, "candidates", one_group_chat)
    monkeypatch.setattr(chat_watch, "new_in", one_message)
    handled = asyncio.run(chat_watch.sweep())

    assert handled == []                        # not his: never forwarded
    from app import delivery
    assert delivery.chase_due(now=2_000_000_000.0) == [], "recorded, but never owed"


def test_a_message_that_IS_his_still_gets_through(monkeypatch):
    """The gate above must not become a way to lose his own messages."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    monkeypatch.setenv("ASTA_CHATWATCH", "1")

    async def one_direct_chat():
        return ["Alex Kumar"]

    async def one_message(chat, advance=True):
        return [{"sender": "Alex Kumar", "sent_at": 1_800_000_000.0,
                 "text": "Schema mapping is pending i think"}]

    monkeypatch.setattr(chat_watch, "candidates", one_direct_chat)
    monkeypatch.setattr(chat_watch, "new_in", one_message)
    monkeypatch.setattr(responder, "respond", lambda *a, **k: None)
    handled = asyncio.run(chat_watch.sweep())
    assert [h["who"] for h in handled] == ["Alex Kumar"]


def test_a_question_he_already_answered_is_not_forwarded_again(monkeypatch):
    """This check used to sit two lines lower — it skipped the investigation and
    forwarded the message anyway. "i have already shared na the analysis then why
    again it doing"."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    monkeypatch.setenv("ASTA_CHATWATCH", "1")

    async def one_direct_chat():
        return ["Alex Kumar"]

    async def one_message(chat, advance=True):
        return [{"sender": "Alex Kumar", "sent_at": 1_800_000_000.0,
                 "text": "Retry fix merged?"}]

    monkeypatch.setattr(chat_watch, "candidates", one_direct_chat)
    monkeypatch.setattr(chat_watch, "new_in", one_message)
    monkeypatch.setattr(chat_watch, "answered_by_him", lambda chat, m: True)
    monkeypatch.setattr(responder, "respond", lambda *a, **k: None)
    assert asyncio.run(chat_watch.sweep()) == []


def test_his_reply_is_found_in_a_long_thread():
    """The bug underneath the bug, and the reason the settle looked correct in a
    test and did nothing in life.

    `store.teams_messages` orders oldest-first and THEN applies the limit, so an
    unwindowed read of a busy thread returns the oldest 200 messages — and a
    reply, which is by definition the newest thing in it, is never among them.
    His Alex thread holds 200+ messages, so "has he answered this?" was False
    for every question in it however fast he replied, and the ledger went on
    chasing him about conversations he had finished minutes earlier.
    """
    ask_at = 3_000.0
    store.save_teams_messages(
        [{"key": f"old-{i}", "chat": "Alex Kumar", "sender": "Alex Kumar",
          "text": f"message {i}", "sent_at": 1_000.0 + i} for i in range(260)]
        + [{"key": "his-reply", "chat": "Alex Kumar", "sender": "Arunkumar K",
            "text": "sorted, thanks", "sent_at": ask_at + 60}])

    assert chat_watch.answered_by_him("Alex Kumar", {"sent_at": ask_at}) is True


def test_a_thread_he_has_not_answered_is_still_open():
    """The other direction — the check must not simply return True."""
    store.save_teams_messages(
        [{"key": f"o-{i}", "chat": "Harini", "sender": "Harini S",
          "text": f"message {i}", "sent_at": 1_000.0 + i} for i in range(260)])
    assert chat_watch.answered_by_him("Frankie", {"sent_at": 3_000.0}) is False


# --- 3. waiting on him for a room he is only sitting in --------------------------------
#
# "im not even in the contest but it still asking it waiting for me" — the five
# items in that screenshot, verbatim.

REAL_BROADCASTS = [
    ("Marlowe Kumar Singh", "Everyone all the new development and bug fixes are "
                           "getting deployed to MDP only so please use the MDP server"),
    ("Facilitator", "About 7 minutes remain; please prioritize event mapping and "
                    "settle the Walmart 3P handling before time is up"),
]

REAL_ASKS = [
    ("Alex Kumar", "Retry fix merged?"),
    ("Alex Kumar", "Schema Mapping is pending i think"),
    ("Kavya Ramesh Iyer", "Arunkumar K share me the bug id"),
    ("Harini S", "okayy.."),
    # The word appears, but he is being TOLD about a room, not addressed as one.
    ("Alex Kumar", "we told everyone about the release already"),
]


@pytest.mark.parametrize("who,text", REAL_BROADCASTS)
def test_a_room_wide_announcement_is_recognised(who, text):
    assert attention.is_broadcast(who, text) is True


@pytest.mark.parametrize("who,text", REAL_ASKS)
def test_a_real_question_is_not_mistaken_for_an_announcement(who, text):
    assert attention.is_broadcast(who, text) is False


def test_an_announcement_is_never_floored_up_to_today():
    """The engaged window says yes to anything in a group he was just talking in,
    and that is exactly how these reached the chase list."""
    pri, why, _ = attention.rank(
        False, REAL_BROADCASTS[0][1], addressed=True, who=REAL_BROADCASTS[0][0])
    assert pri >= attention.P_FYI
    assert "announcement" in why


def test_being_tagged_still_reaches_him():
    """The floor that got four people through to his phone must survive this."""
    pri, why, _ = attention.rank(False, "Arunkumar K can you check this",
                                 addressed=True, who="Alex Kumar")
    assert pri <= attention.P_TODAY


def test_the_investigator_and_the_ranker_share_one_definition():
    """Two copies of "is this a broadcast" is how one of them drifts."""
    assert responder.is_broadcast is attention.is_broadcast


def test_an_assigned_incident_is_never_demoted_as_a_broadcast():
    """The trap in wiring this into ranking. `is_broadcast` also asks
    `from_bulk_sender`, whose list contains `servicenow` — so ranking on it
    demoted "INC4471 booking service down" to FYI and it stopped reaching him.
    That exemption failing to survive to the ranking layer is a bug this codebase
    has already had once."""
    pri, _, _ = attention.rank(True, "INC4471 booking service down",
                               addressed=True, who="ServiceNow")
    assert pri <= attention.P_TODAY


def test_an_outage_announced_to_a_room_is_still_an_outage():
    """Criticality outranks the room. Being wrong the other way costs him an
    outage he was told about in the wrong words."""
    pri, _, _ = attention.rank(True, "Everyone: production is down, payments failing",
                               addressed=True, who="Marlowe Kumar Singh")
    assert pri <= attention.P_TODAY


def test_the_repair_clears_what_the_old_behaviour_left_behind(monkeypatch):
    """A one-off pass over the backlog, applying today's rules to yesterday's
    rows. On his live ledger it settles 719 of 1,844: 169 of Asta's own notices,
    114 announcements, and 436 he had already replied to."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    # `asta` is the source Asta stamps on its own notices — see SELF_SOURCE.
    attention.consider(attention.SELF_SOURCE, "r-self", who="",
                       what="✅ DONE — #88 mapping", priority=1)
    attention.consider("teams-chat", "r-bcast", who="Marlowe Kumar Singh",
                       what="Marlowe Kumar Singh: Everyone all the new development "
                            "and bug fixes are getting deployed to MDP", priority=1)
    attention.consider("teams-chat", "r-open", who="Alex Kumar",
                       what="Alex Kumar: Schema mapping pending?", priority=1)

    assert attention.reconcile() == 2
    assert store.attention_get("r-self")["state"] == "dropped"
    assert store.attention_get("r-bcast")["state"] == "dropped"
    # A real, unanswered question is not swept up with them.
    assert store.attention_get("r-open")["state"] not in ("acted", "dropped")


def test_the_repair_is_safe_to_run_twice():
    """Settled rows are settled; a second pass must not re-count or re-label."""
    attention.reconcile()
    assert attention.reconcile() == 0


# --- 4. everything arrived the same colour -----------------------------------------------
#
# 365 of the last 571 Teams pushes carried 🔴. The ledger ranks four ways; the
# message showed two, split so the biggest bucket was always red.

def test_each_rank_gets_its_own_mark():
    marks = [attention.marker(p) for p in
             (attention.P_NOW, attention.P_TODAY, attention.P_FYI, attention.P_MUTE)]
    assert len(set(marks)) == 4, f"a ladder with repeats is not a ladder: {marks}"


def test_an_unranked_line_claims_nothing():
    assert attention.marker(None) == "·"


def test_an_out_of_range_rank_does_not_crash():
    assert attention.marker(-5) and attention.marker(99)


def test_the_line_on_his_phone_uses_the_ladder():
    assert chat_watch.render("V", "V", "hi", attention.P_NOW).startswith("🚨")
    assert chat_watch.render("V", "V", "hi", attention.P_FYI).startswith("🟡")


# --- 5. "Someone is calling" ---------------------------------------------------------------
#
# Nine alerts in three days, one at 00:21, not one with a name.

def test_a_call_already_in_progress_is_not_a_ring():
    """This clause is what fired nine times: it is what a JOINED call renders."""
    assert incoming.looks_incoming("3 others are in this call") is False
    assert incoming.looks_incoming("2 participants in the call") is False


def test_a_real_ring_is_still_recognised():
    assert incoming.looks_incoming("Alex Kumar is calling you") is True
    assert incoming.looks_incoming("Incoming call from Meera") is True


@pytest.mark.parametrize("text,want", [
    ("Alex Kumar is calling you", "Alex Kumar"),
    ("Incoming call from Meera Iyer", "Meera Iyer"),
])
def test_the_caller_is_named(text, want):
    assert incoming.who_is_calling(text) == want


def test_a_nameless_ring_is_never_pushed():
    """"who calling u have to tell na without them how i can decide ?" — the
    question without the one fact needed to answer it."""
    class _Page:
        async def evaluate(self, _js):
            return "incoming call"          # the words, but nobody's name

    assert asyncio.run(incoming.look(_Page())) is None


def test_a_named_ring_still_gets_through():
    class _Page:
        async def evaluate(self, _js):
            return "Alex Kumar is calling you"

    call = asyncio.run(incoming.look(_Page()))
    assert call and call["who"] == "Alex Kumar"


# --- 6. he heard about messages minutes late -------------------------------------------------
#
# Measured over a week: 433 of 2,743 messages more than 15 minutes late, 24% over
# five. `moved_up` existed the whole time and `pick` never asked it.

#: Four chats got a message; the rotation is partway through the tail. This is
#: the case the tier exists for and the only one that distinguishes it — the
#: fourth mover sits just past ALWAYS_TOP, so without it "I" waits for the cursor
#: to come round, which over twenty threads is several sweeps.
_BUSY_PREV = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
_BUSY_NOW = ["F", "G", "H", "I", "A", "B", "C", "D", "E", "J"]


def test_a_chat_that_just_moved_is_opened_first():
    chosen, _ = chat_watch.pick(_BUSY_NOW, cursor=0, previous=_BUSY_PREV)
    assert chosen[0] == "F"


@pytest.mark.parametrize("cursor", [3, 5])
def test_activity_beats_the_rotation(cursor):
    """The whole latency fix. Without the moved-up tier this chat is not opened
    at all this sweep, and waits for the cursor to reach it."""
    with_history, _ = chat_watch.pick(_BUSY_NOW, cursor, _BUSY_PREV)
    blind, _ = chat_watch.pick(_BUSY_NOW, cursor, [])
    assert "I" in with_history
    assert "I" not in blind, "the test is not exercising the tier"


def test_a_chat_deep_in_the_tail_no_longer_waits_for_the_rotation():
    """H is last in a rail of eight. With ROTATE=2 it was up to four sweeps away;
    a message moves it to the top and it is read on the next one."""
    previous = ["A", "B", "C", "D", "E", "F", "G", "H"]
    current = ["H", "A", "B", "C", "D", "E", "F", "G"]
    chosen, _ = chat_watch.pick(current, cursor=0, previous=previous)
    assert "H" in chosen


def test_the_head_is_still_read_every_sweep():
    """A burst in one room must not starve the rest."""
    previous = ["A", "B", "C", "D", "E"]
    current = ["E", "A", "B", "C", "D"]
    chosen, _ = chat_watch.pick(current, cursor=0, previous=previous)
    assert {"E", "A", "B"} <= set(chosen)


def test_no_history_behaves_as_it_always_did():
    """First sweep after a restart: nothing has "moved" yet."""
    current = ["A", "B", "C", "D", "E", "F"]
    assert chat_watch.pick(current, 0, [])[0] == chat_watch.pick(current, 0)[0]


def test_the_cap_on_chats_opened_per_sweep_still_holds():
    """Each open is a real navigation on a profile that tolerates one writer."""
    previous = list("ABCDEFGHIJ")
    current = list("JIHGFEDCBA")                 # everything moved
    chosen, _ = chat_watch.pick(current, 0, previous)
    assert len(chosen) <= chat_watch.MAX_OPENS


# --- 7. a cancelled meeting ------------------------------------------------------------------

@pytest.mark.parametrize("title", ["Canceled: Sprint review", "Cancelled: 1:1 with Priya",
                                   "  canceled:  Ideation"])
def test_a_called_off_meeting_is_recognised(title):
    assert outlook.is_cancelled(title) is True


@pytest.mark.parametrize("title", ["Sprint review", "Cancellation policy review",
                                   "Discuss cancelled bookings"])
def test_a_live_meeting_is_not(title):
    assert outlook.is_cancelled(title) is False


def test_a_cancelled_meeting_leaves_the_calendar_entirely():
    """No prep offer, no join offer, nothing — "meeting is cancelled ... for that
    u dont have to ask do i have to prepare anything"."""
    rows = ["Canceled: Ideation, 3:00 PM to 4:00 PM, Busy, By Alex Kumar",
            "Sprint review, 2:00 PM to 3:00 PM, Busy, By Sam"]
    assert [e["title"] for e in outlook._events_from(rows)] == ["Sprint review"]


def test_a_meeting_that_merely_mentions_cancelling_stays():
    """Over-filtering the calendar is how he misses a meeting he had to be at."""
    rows = ["Discuss cancelled bookings, 2:00 PM to 3:00 PM, Busy, By Sam"]
    assert len(outlook._events_from(rows)) == 1


# --- 8. the same sentence every time ------------------------------------------------------------

def test_the_acknowledgement_says_what_it_is_doing():
    from app import main
    said = {main._working_note("none", t) for t in (
        "can you debug why the booking failed",
        "review the PR comments in 1409",
        "production is down for bulk booking")}
    assert len(said) == 3, f"one sentence for every ask is the bug: {said}"


def test_it_names_the_task_it_just_started():
    from app import main
    conv = store.create_conversation(model="copilot", workspace=None)
    task = store.create_task("Implement transportAssetPriority mapping",
                             "code", "do it", None)
    tid = task["id"]
    tasks.link_task(conv["id"], tid)
    note = main._working_note(conv["id"], "implement it")
    assert f"#{tid}" in note and "transportAssetPriority" in note
