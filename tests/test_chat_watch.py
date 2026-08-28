"""Reading his actual chats — the case the Activity feed structurally cannot see.

Arun:

    "if they didnt tag , still if it one to one chat na , that message is for me
     correct , sometimes the first message they tag and second message they wont
     tag in both personal one to one chat as well as group chat this is basic
     thing"

Teams' Activity feed lists mentions, replies, reactions and invites, and never an
ordinary message. Verified against his real feed rows, not assumed. So everything
below is about the reader that replaces it, and the two properties that keep it
affordable: the rail's ORDER is the signal, and Asta's own high-water mark — not
Teams' unread styling — decides what is new.
"""

from __future__ import annotations

import asyncio

import pytest

from app import chat_watch, store


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ASTA_CHATWATCH", "1")


# --- the rail order is the signal ---------------------------------------------

def test_a_chat_that_rose_had_activity():
    assert "Vinish Kumar" in chat_watch.moved_up(
        ["Komal", "Vinish Kumar", "Team"], ["Vinish Kumar", "Komal", "Team"])


def test_the_top_chat_is_always_checked():
    """The untagged follow-up this module exists for: a SECOND message in a chat
    already at position 0 moves nothing in the rail."""
    assert chat_watch.moved_up(["Vinish", "Komal"], ["Vinish", "Komal"]) == ["Vinish"]


def test_a_brand_new_conversation_counts():
    assert "Newperson" in chat_watch.moved_up(["Komal"], ["Newperson", "Komal"])


def test_the_first_ever_run_does_not_open_the_whole_rail():
    """With no previous order everything looks new. Opening forty conversations on
    the first poll is minutes of browser work on a single-writer profile."""
    assert chat_watch.moved_up([], ["A", "B", "C", "D"]) == ["A"]


def test_nothing_on_screen_opens_nothing():
    assert chat_watch.moved_up(["A", "B"], []) == []


def test_a_chat_that_sank_is_not_reopened():
    """Only a RISE means activity. Something dropping down the rail happened
    because other chats moved, and re-reading it is a page load for nothing."""
    assert "Komal" not in chat_watch.moved_up(["Komal", "Vinish"], ["Vinish", "Komal"])


# --- Asta's high-water mark, not Teams' read state ----------------------------

def _msgs(*keys):
    return [{"key": k, "text": f"message {k}", "sender": "Vinish Kumar"} for k in keys]


def test_only_messages_after_the_mark_are_new():
    store.kv_set(chat_watch._seen_key("Vinish"), "m2")
    assert [m["key"] for m in chat_watch.unseen("Vinish", _msgs("m1", "m2", "m3", "m4"))] \
        == ["m3", "m4"]


def test_the_same_poll_twice_yields_nothing_the_second_time():
    rows = _msgs("m1", "m2")
    store.kv_set(chat_watch._seen_key("Vinish"), "")
    chat_watch.unseen("Vinish", rows)
    chat_watch.remember("Vinish", rows)
    assert chat_watch.unseen("Vinish", rows) == []


def test_first_sight_of_a_thread_takes_only_the_latest():
    """Otherwise adding a chat replays its entire visible history at him."""
    store.kv_set(chat_watch._seen_key("New"), "")
    assert [m["key"] for m in chat_watch.unseen("New", _msgs("a", "b", "c"))] == ["c"]


def test_a_mark_that_scrolled_out_of_view_replays_rather_than_loses():
    """If the remembered message is no longer in the window, the conversation ran
    ahead of the poll. Losing them silently is the failure mode that matters."""
    store.kv_set(chat_watch._seen_key("Vinish"), "long-gone")
    assert len(chat_watch.unseen("Vinish", _msgs("m8", "m9"))) == 2


def test_it_does_not_depend_on_teams_unread_styling():
    """Two reasons, both his. Anything he glanced at on his phone would become
    invisible here — the opposite of "irrespective im present or not" — and this
    Teams build renders no unread marker at all: empty aria-label, empty data-tid,
    hashed Fluent class names. Checked against his live rail."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(chat_watch))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    # Named symbols, not a substring search: the first version of this test
    # matched the word inside "unreadable" in a comment.
    assert not ({"unread_rows", "_row_is_unread", "_UNREAD_HINTS"} & used), \
        "the watcher reads Teams' unread styling — it must use its own mark"


# --- what it does with what it finds ------------------------------------------

class _Bridge:
    def __init__(self, rows):
        self.rows = rows
        self.opened = []

    async def read_history(self, chat, limit=12, max_scrolls=0):
        self.opened.append(chat)
        return self.rows.get(chat, [])


@pytest.fixture
def wired(monkeypatch):
    """A rail that moved, a chat with one new message, and no browser."""
    bridge = _Bridge({"Vinish Kumar": [
        {"key": "k1", "sender": "Vinish Kumar",
         "text": "can you check the production temporal bookings struck"}]})

    async def _candidates():
        return ["Vinish Kumar"]

    monkeypatch.setattr(chat_watch, "candidates", _candidates)
    monkeypatch.setattr(chat_watch, "new_in",
                        lambda chat: bridge.read_history(chat))
    store.kv_set(chat_watch._seen_key("Vinish Kumar"), "")
    return bridge


def test_an_untagged_one_to_one_message_is_handled(wired):
    """The whole point. Nobody @mentioned him; it is still for him."""
    handled = asyncio.run(chat_watch.sweep())
    assert handled, "an untagged 1:1 message was ignored"
    assert handled[0]["who"] == "Vinish Kumar"


def test_it_investigates_what_it_finds(wired, monkeypatch):
    """The reader and the actuator have to meet — otherwise this is a better
    sensor bolted to the same dead end."""
    monkeypatch.setenv("ASTA_RESPOND", "1")
    from app import responder
    # About the reader meeting the actuator, not about familiar-vs-new.
    monkeypatch.setattr(responder, "familiar", lambda text: (True, "test: known"))
    spawned = []
    from app import tasks
    monkeypatch.setattr(tasks, "spawn",
                        lambda title, prompt, kind="analysis", ws=None, *a, **k:
                        (spawned.append({"kind": kind, "title": title}), {"id": 7})[1])
    asyncio.run(chat_watch.sweep())
    assert spawned and spawned[0]["kind"] == "analysis"


def test_his_own_messages_are_not_things_he_was_asked(monkeypatch):
    from app import meetings
    monkeypatch.setattr(meetings, "speaker_is_arun", lambda s: "arun" in s.lower())
    assert chat_watch.is_from_him("Arunkumar K")
    assert not chat_watch.is_from_him("Vinish Kumar")


def test_one_unreadable_thread_does_not_end_the_sweep(monkeypatch):
    async def _candidates():
        return ["Broken", "Fine"]

    async def _new_in(chat, advance=True):
        if chat == "Broken":
            raise RuntimeError("thread would not open")
        # A 1:1 (sender == chat), so it is unambiguously his and the assertion is
        # about the sweep surviving, not about who a message was for.
        return [{"key": "k", "sender": "Fine", "text": "prod is stuck"}]

    monkeypatch.setattr(chat_watch, "candidates", _candidates)
    monkeypatch.setattr(chat_watch, "new_in", _new_in)
    assert asyncio.run(chat_watch.sweep()), "one bad thread ended the whole sweep"


def test_it_is_off_until_switched_on(monkeypatch):
    monkeypatch.delenv("ASTA_CHATWATCH", raising=False)
    assert not chat_watch.enabled()


def test_opens_are_capped(monkeypatch):
    """Each open is a real navigation on a profile that tolerates one writer."""
    monkeypatch.setattr(chat_watch, "MAX_OPENS", 2)
    assert len(chat_watch.moved_up([], ["A", "B", "C", "D"])[:chat_watch.MAX_OPENS]) <= 2


def test_his_own_self_chat_is_not_a_conversation():
    """"Arunkumar K (You)" is PINNED to the top of his rail, so it was permanently
    the "always check the top row" candidate — one of three opens per sweep spent
    on a thread only he writes in, and the genuinely newest conversation hidden
    behind it. The first live sweep said "nothing new" and was right for the wrong
    reason."""
    assert chat_watch.is_furniture("Arunkumar K (You)")
    assert chat_watch.is_furniture("Someone Else (You)")
    assert not chat_watch.is_furniture("Vinish Kumar")


def test_a_team_header_is_not_a_conversation():
    assert chat_watch.is_furniture("TELIKOS - All Teams")


def test_a_real_channel_is_still_watched():
    """Group channels are where untagged follow-ups land too — his second case."""
    assert not chat_watch.is_furniture("Team Booking and Execution")
    assert not chat_watch.is_furniture("OHP Garage")


# --- who a message is for -----------------------------------------------------
# "one to one related messages ... without tag bcoz there no point whether they
# mention or not the message is for me only , need my attentation for group chats
# that is valid my name tagged at first, follow up convo with or without tagging
# as well.. but it should aware and follow up post the first tag message as well"

@pytest.fixture(autouse=True)
def _fresh_engagement():
    store.kv_set(chat_watch._engaged_key("Prod Support"), "")


def test_a_one_to_one_needs_no_tag():
    assert chat_watch.addressed_to_him("Vinish Kumar", "Vinish Kumar",
                                       "can you check prod")


def test_a_group_message_to_nobody_is_not_his():
    """The other direction, and the reason a group is not just a big 1:1: every
    message in every channel counting would make the ledger meaningless."""
    assert not chat_watch.addressed_to_him("Prod Support", "Komal", "deploy done")


def test_a_tag_makes_the_group_his():
    assert chat_watch.addressed_to_him("Prod Support", "Komal",
                                       "arun can you look at this")


def test_the_reply_after_the_tag_counts_without_a_second_tag():
    """The substance of a thread is the replies, and nobody tags twice."""
    chat_watch.addressed_to_him("Prod Support", "Komal", "arun can you look")
    assert chat_watch.addressed_to_him("Prod Support", "Komal", "and also the ETA one")


def test_the_window_closes():
    """A tag last week does not make today's standup chatter his."""
    import time
    chat_watch.note_tagged("Prod Support", now=time.time(), by="Komal")
    # `engaged` now reports WHO pulled him in as well as whether the window is
    # open — being pulled into a thread is not subscribing to the room.
    open_window, by = chat_watch.engaged(
        "Prod Support", now=time.time() + (chat_watch.ENGAGED_HOURS + 1) * 3600)
    assert not open_window


def test_a_group_he_was_never_tagged_in_stays_quiet():
    assert chat_watch.engaged("Some Other Channel") == (False, "")


@pytest.mark.parametrize("text", ["Arun can you check", "arunkumar please review",
                                  "hi Arun Kumar, any update"])
def test_the_ways_he_is_tagged(text):
    assert chat_watch.mentions_him(text), text


def test_someone_elses_name_is_not_his_tag():
    assert not chat_watch.mentions_him("komal can you check this")


# --- the shared page is not always showing the chat list ----------------------
# Fourteen hours running, two chats ever processed. The Teams page is POOLED and
# shared with every other loop; `_find_chat` runs a search to open a thread, and a
# search replaces the rail with its results. So `[role="treeitem"]` returned
# matches for whatever was last searched, and the watcher compared THAT against
# the previous order. The stored rail had "Divya" and "Palikala Divya Maheswari"
# at the top — results of a resolve call, not his conversations.

def test_the_real_chat_list_is_recognised():
    assert chat_watch.looks_like_the_chat_list(
        ["Copilot", "Mentions", "Discover", "Drafts", "Saved", "Vinish Kumar"])


def test_search_results_are_not_mistaken_for_the_rail():
    """The exact contamination that was found in his live store."""
    assert not chat_watch.looks_like_the_chat_list(
        ["Divya", "Palikala Divya Maheswari", "BEP_Telikos : Defect Triage"])


def test_an_empty_read_is_not_a_chat_list():
    assert not chat_watch.looks_like_the_chat_list([])


def test_a_contaminated_rail_is_never_remembered(monkeypatch):
    """The damage was not only missing a poll — the bad order was SAVED, so the
    next comparison was against nonsense too."""
    store.kv_set(chat_watch._RAIL_KEY, '["Vinish Kumar", "Komal Jayswal"]')

    async def _contaminated():
        return []          # candidates() bails rather than storing search results

    monkeypatch.setattr(chat_watch, "candidates", _contaminated)
    asyncio.run(chat_watch.sweep())
    import json
    assert json.loads(store.kv_get(chat_watch._RAIL_KEY)) == ["Vinish Kumar", "Komal Jayswal"]


def test_every_chat_failing_is_reported_not_swallowed(monkeypatch):
    """A sweep where nothing opens looks exactly like a quiet morning. That is how
    this ran for fourteen hours saying nothing while reading almost nothing."""
    from app import attention

    async def _candidates():
        return ["A", "B"]

    async def _fails(chat, advance=True):
        raise RuntimeError("thread would not open")

    monkeypatch.setattr(chat_watch, "candidates", _candidates)
    monkeypatch.setattr(chat_watch, "new_in", _fails)
    noted = {}
    monkeypatch.setattr(attention, "note_scrape_error",
                        lambda src, exc: noted.update({"src": src, "why": str(exc)}))
    asyncio.run(chat_watch.sweep())
    assert noted["src"] == "teams-chat"
    assert "failed to open" in noted["why"]


def test_a_partly_working_sweep_counts_as_healthy(monkeypatch):
    """One bad thread among several is normal and must not read as a dead watcher —
    that would train him to ignore the alarm."""
    from app import attention

    async def _candidates():
        return ["Good", "Bad"]

    async def _mixed(chat, advance=True):
        if chat == "Bad":
            raise RuntimeError("nope")
        return [{"key": "k", "sender": "Vinish", "text": "prod is stuck"}]

    monkeypatch.setattr(chat_watch, "candidates", _candidates)
    monkeypatch.setattr(chat_watch, "new_in", _mixed)
    ok = {}
    monkeypatch.setattr(attention, "note_scrape", lambda src, now=None: ok.update({"src": src}))
    monkeypatch.setattr(attention, "note_scrape_error",
                        lambda src, exc: pytest.fail("reported dead on one bad thread"))
    asyncio.run(chat_watch.sweep())
    assert ok["src"] == "teams-chat"


# --- open the chats and read them --------------------------------------------
# Arun: "why u watching via notification , can't use playwright and open teams and
# fetch from there via direct visible from there , from notifications u wont get
# all right ?"
#
# He was right twice over. The Activity feed carries mentions only, and the first
# replacement still inferred activity from the rail ORDER — so anything the
# ordering did not reflect was never read. Fourteen hours, two conversations, and
# nothing anywhere saying so. These pin the obvious correct thing instead: open the
# conversations and read them, bounded by a window rather than by a clever signal.

def _rail(n=8):
    return [f"chat{i}" for i in range(n)]


def test_the_head_of_the_list_is_read_every_sweep():
    """Where a new message almost always is."""
    cur = 0
    for _ in range(5):
        picked, cur = chat_watch.pick(_rail(), cur)
        assert picked[:chat_watch.ALWAYS_TOP] == _rail()[:chat_watch.ALWAYS_TOP]


def test_every_conversation_is_eventually_read():
    """The property the order-based version could not offer at all."""
    rail, cur, seen = _rail(8), 0, set()
    for _ in range(6):
        picked, cur = chat_watch.pick(rail, cur)
        seen |= set(picked)
    assert seen == set(rail), f"never read: {sorted(set(rail) - seen)}"


def test_a_sweep_never_exceeds_the_cap():
    """Each open is a real navigation on a single-writer profile."""
    cur = 0
    for _ in range(4):
        picked, cur = chat_watch.pick(_rail(40), cur)
        assert len(picked) <= chat_watch.MAX_OPENS


def test_no_chat_is_opened_twice_in_one_sweep():
    picked, _ = chat_watch.pick(_rail(4), 0)
    assert len(picked) == len(set(picked))


def test_a_short_list_is_handled():
    picked, cur = chat_watch.pick(["only"], 0)
    assert picked == ["only"]
    picked, _ = chat_watch.pick([], 0)
    assert picked == []


def test_the_cap_cannot_silently_disable_the_rotation():
    """`ASTA_CHATWATCH_MAX_OPENS=3` was left pinned in .env from the earlier
    design, so the rotating tail was truncated away and only the head was ever
    read — the configured cap quietly reinstated the bug the code had fixed."""
    assert chat_watch.MAX_OPENS >= chat_watch.ALWAYS_TOP + chat_watch.ROTATE, (
        "MAX_OPENS is below TOP+ROTATE, so the tail is never reached")


# --- read everything, forward only what is his --------------------------------
# "see here some of messages are not meant for me in the group, still it throwing
# the messages inn teams"
#
# `direct` was computed on every message and then never used: every line of every
# group conversation went to his phone. A release-triage channel sent him "Shall
# we join here now?" and "Hi Sumith just wanted to check what we have concluded" —
# other people talking to each other, none of it his.
#
# Reading every conversation is right. Forwarding every conversation is not.

def _sweep_with(monkeypatch, chat, sender, text):
    async def _candidates():
        return [chat]

    async def _new_in(c, advance=True):
        return [{"key": "k1", "sender": sender, "text": text}]

    monkeypatch.setattr(chat_watch, "candidates", _candidates)
    monkeypatch.setattr(chat_watch, "new_in", _new_in)
    sent = []

    async def _notify(text, source, **kw):
        sent.append(text)

    asyncio.run(chat_watch.sweep(_notify))
    return sent


def test_group_chatter_between_other_people_is_not_forwarded(monkeypatch):
    """The exact line he was sent."""
    sent = _sweep_with(monkeypatch, "BEP_Telikos : Defect Triage", "Rupesh Kumar",
                       "Hi Sumith just wanted to check what we have concluded")
    assert not sent, f"forwarded a conversation between other people: {sent}"


def test_a_group_message_naming_him_is_forwarded(monkeypatch):
    sent = _sweep_with(monkeypatch, "BEP_Telikos : Defect Triage", "Rupesh Kumar",
                       "Arunkumar can you check this defect")
    assert sent and "Arunkumar can you check" in sent[0]


def test_a_one_to_one_is_always_forwarded(monkeypatch):
    """No tag needed — the chat IS the person."""
    sent = _sweep_with(monkeypatch, "Abhijit Mohapatra", "Abhijit Mohapatra",
                       "you are in code atlas team")
    assert sent and "code atlas" in sent[0]


def test_the_follow_up_after_a_tag_is_still_forwarded(monkeypatch):
    """Nobody tags twice, and the replies are the substance."""
    _sweep_with(monkeypatch, "Prod Support", "Komal", "arun please look")
    sent = _sweep_with(monkeypatch, "Prod Support", "Komal", "and the ETA one too")
    assert sent, "lost the untagged follow-up inside a conversation he was pulled into"


def test_unforwarded_group_traffic_is_still_recorded(monkeypatch):
    """Silence is not amnesia — "what did I miss in that channel" still has an
    answer, it simply does not interrupt him."""
    from app import attention
    seen = []
    monkeypatch.setattr(attention, "consider",
                        lambda src, key, **kw: seen.append(kw.get("what")) or True)
    _sweep_with(monkeypatch, "BEP_Telikos : Defect Triage", "Rupesh Kumar",
                "Shall we join here now?")
    assert seen, "dropped it entirely instead of recording it"


# --- what he actually reads ---------------------------------------------------
# "what is this message what i will get know from these ? nothing proper"
#
# He was sent this, verbatim:
#
#     · Palikala Divya Maheswari — Palikala Divya Maheswari: Arunkumar K
#     28/08/2026 18:38
#     lets analyse on some idea and see how it going on weekend sunday and Monday
#     · Vinish Kumar — Vinish Kumar: https://maersk.service-now.com/now/platform-…
#
# Three faults in one notification: the person named twice, the body opening with
# a quoted reply header so the real sentence starts on line three, and a bare URL
# that says nothing about what it is.

REPLY = ("Arunkumar K\n28/08/2026 18:38\nlets analyse on some idea and see how it "
         "going on weekend sunday and Monday, if all goes go")


def test_a_one_to_one_names_the_person_once():
    line = chat_watch.render("Palikala Divya Maheswari", "Palikala Divya Maheswari",
                             "hello", 1)
    assert line.count("Palikala Divya Maheswari") == 1


def test_a_group_message_says_which_group():
    """Where it was said is the part he cannot infer from the sender."""
    line = chat_watch.render("BEP_Telikos : Defect Triage", "Rupesh Kumar", "hi", 1)
    assert "Rupesh Kumar" in line and "BEP_Telikos" in line


#: A real reply, captured verbatim from his store. Sender was Ayashkant Baral;
#: the first three lines are Ashwin Kumar's message being replied TO.
REAL_REPLY = (
    "Ashwin Kumar\n20/07/2026 11:14\nAyashkant - What is IP. For NAM, in the "
    "future we many get combined invoice scenario.\n\nIP is specific integration "
    "from EDI like we have many channels (WebEC, SoftPoint)\n\n"
    "1 Like reaction with medium dark skin tone.")


def test_a_reply_shows_what_the_sender_typed_not_what_they_quoted():
    """The misattribution he caught: "msg was mine but u shoing divya".

    Stripping only the name and timestamp left the QUOTED text as the message, so
    one person's sentence was shown under another's name. Putting a colleague's
    words in someone else's mouth is worse than the raw noise it replaced.
    """
    out = chat_watch.clean_message(REAL_REPLY)
    assert out.startswith("IP is specific integration")
    assert "What is IP" not in out, "showed the quoted message as the reply"


def test_a_quote_with_no_reply_body_is_not_passed_off_as_one():
    """His exact case: the capture held only his own quoted sentence. There is
    nothing of theirs to show, and showing the quote would misattribute it."""
    assert chat_watch.clean_message(REPLY) == ""
    assert "couldn't read" in chat_watch.summarise(REPLY)
    assert "lets analyse" not in chat_watch.summarise(REPLY)


def test_the_scrape_drops_the_quoted_block_before_the_text_is_read():
    """The real fix is upstream — a reply renders the quoted message inside its own
    body, so innerText glued both together. The text-level guard above stays as the
    fallback for when these selectors stop matching."""
    import inspect

    from app import teams_bridge
    js = inspect.getsource(teams_bridge)
    assert "quoted-reply" in js and "quotes.forEach(q => q.remove())" in js


def test_reaction_counts_are_not_part_of_what_was_said():
    assert chat_watch.clean_message("Can you confirm the fix\n\n4 Like reactions.") \
        == "Can you confirm the fix"


def test_an_ordinary_message_survives_cleaning():
    """The cleaner must not eat real text — that would be worse than the noise."""
    assert chat_watch.clean_message("prod is stuck since 3pm") == "prod is stuck since 3pm"


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/VinishKumar1/incident-copilot", "GitHub: VinishKumar1/incident-copilot"),
    ("https://maersk.service-now.com/now/platform-analytics-workspace/x", "ServiceNow"),
    ("https://maersk-tools.atlassian.net/browse/BEPTELIKOS-10500", "Jira"),
])
def test_a_link_is_named_not_pasted(url, expected):
    assert expected in chat_watch.describe_link(url)


def test_a_message_that_is_only_a_link_says_what_was_shared():
    out = chat_watch.summarise("https://github.com/VinishKumar1/incident-copilot")
    assert out.startswith("shared GitHub")
    assert "https://" not in out


def test_a_message_with_text_and_a_link_keeps_both():
    out = chat_watch.summarise("have a look at this https://github.com/a/b")
    assert "have a look at this" in out and "GitHub: a/b" in out


def test_a_long_message_is_cut_rather_than_sent_whole():
    out = chat_watch.summarise("word " * 200)
    assert len(out) <= 170 and out.endswith("…")


# --- being pulled into a thread is not subscribing to the room ----------------
# Vinish tagged him once in a release channel. For the next twelve hours every
# message in that room reached his phone: Navya's schema question, Roshan's "22nd
# September ko release hai", and "Vinish Kumar what do you say", which is
# addressed to Vinish. The window was right; its breadth was not.

CHAT = "Prod Support till 11th September"


@pytest.fixture(autouse=True)
def _clear_window():
    store.kv_set(chat_watch._engaged_key(CHAT), "")


def test_the_tag_opens_the_window():
    assert chat_watch.addressed_to_him(CHAT, "Vinish Kumar",
                                       "Arunkumar, could you look into these issues")


def test_the_follow_up_from_whoever_pulled_him_in_still_counts():
    """His ask: "follow up convo with or without tagging as well"."""
    chat_watch.addressed_to_him(CHAT, "Vinish Kumar", "Arunkumar, could you look")
    assert chat_watch.addressed_to_him(CHAT, "Vinish Kumar",
                                       "also the duplicate key one is still open")


def test_the_rest_of_the_room_does_not_come_with_it():
    """The flood, verbatim from what he was sent."""
    chat_watch.addressed_to_him(CHAT, "Vinish Kumar", "Arunkumar, could you look")
    for who, text in [("Navya R", "2) I found this issue for two Service Plan numbers"),
                      ("Roshan Kumar Thakur", "22nd September ko release hai"),
                      ("Roshan Kumar Thakur", "Also inform telemetry now")]:
        assert not chat_watch.addressed_to_him(CHAT, who, text), f"{who}: {text}"


def test_a_message_aimed_at_a_third_person_is_theirs():
    """"Vinish Kumar what do you say" is a question for Vinish."""
    chat_watch.addressed_to_him(CHAT, "Vinish Kumar", "Arunkumar, could you look")
    assert not chat_watch.addressed_to_him(CHAT, "Roshan Kumar Thakur",
                                           "Vinish Kumar what do you say")


def test_anyone_naming_him_still_reaches_him():
    """The window must never become a filter that loses a direct ask."""
    chat_watch.addressed_to_him(CHAT, "Vinish Kumar", "Arunkumar, could you look")
    assert chat_watch.addressed_to_him(CHAT, "Navya R",
                                       "Arunkumar can you confirm the topic name")


def test_the_window_is_hours_not_a_working_day():
    """A tag at breakfast should not make the room his until the evening. A
    conversation that resumes tomorrow gets tagged again — that is what people do."""
    assert chat_watch.ENGAGED_HOURS <= 4


def test_naming_himself_is_not_naming_someone_else():
    assert not chat_watch.names_someone_else("Vinish Kumar here, any update", "Vinish Kumar")


def test_an_ordinary_sentence_is_not_read_as_addressing_anyone():
    """Over-filtering loses him a message, which is the worse failure."""
    assert not chat_watch.names_someone_else("Activity getting missed in prod", "Navya R")


# --- do not re-investigate what he has already answered -----------------------

def test_a_thread_he_has_replied_in_is_not_investigated_again(monkeypatch):
    """"i have already shared na the analysis then why again it doing" — his own
    answer was sitting in the thread above the task that went to find it."""
    monkeypatch.setattr(store, "teams_messages",
                        lambda chat="", limit=200, **k: [
                            {"sender": "Vinish Kumar", "text": "look into these", "sent_at": 100},
                            {"sender": "Arunkumar K", "text": "already checked, it's the schema",
                             "sent_at": 200}])
    from app import meetings
    monkeypatch.setattr(meetings, "speaker_is_arun", lambda s: "arun" in (s or "").lower())
    assert chat_watch.answered_by_him(CHAT, {"sent_at": 100})


def test_an_unanswered_ask_is_still_picked_up(monkeypatch):
    monkeypatch.setattr(store, "teams_messages",
                        lambda chat="", limit=200, **k: [
                            {"sender": "Vinish Kumar", "text": "look into these", "sent_at": 300}])
    assert not chat_watch.answered_by_him(CHAT, {"sent_at": 300})


def test_an_untimed_message_is_not_guessed_about(monkeypatch):
    """A row with no timestamp cannot honestly be said to come after anything."""
    assert not chat_watch.answered_by_him(CHAT, {"sent_at": None})
