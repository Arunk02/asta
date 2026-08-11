"""Who a message reaches, and whether the words he approved are the words that go.

Two failures live here and they share an ending: Arun believes something left the
machine that did not, or believes it went to the person it did not go to. Neither
announces itself. He finds out from the colleague who never replied.

The first is targeting. Teams' search returns filter chips, people, files, group
chats, meetings and Loop cards all as `[role="option"]`, and the old code took the
first person-shaped row that matched any token. "Kumar" quietly opened Vinish
Kumar while Abhijit, Anil and Roshan Kumar sat in the same list.

The second is the send itself. An approved Teams draft used to be handed BACK to a
brain as a prompt saying "send this now", which is exactly what `ops.py` exists to
prevent for Jira. A brain asked to re-perform a send can reword it, address it to
a different person of that name, treat the tool call as optional, or answer ABOUT
sending instead of sending. All four end with no message arriving.
"""

from __future__ import annotations

import asyncio

import pytest

from app import loop, main, ops, teams_bridge


# --- picking one target out of many -----------------------------------------

def _opt(i, aria, text):
    return {"i": i, "aria": aria, "text": text}


def test_one_match_is_simply_used():
    picked = teams_bridge._one_of([_opt(6, "Person  Vinish Kumar", "Vinish Kumar")],
                                  "Vinish", "people")
    assert picked["i"] == 6


def test_a_surname_matching_several_people_is_refused_not_guessed():
    """The real one. Every Kumar in the directory matches, and picking the first
    sends Arun's message to whoever Teams happened to rank highest."""
    people = [_opt(1, "Person  Vinish Kumar", "Vinish Kumar\n\nSOFTWARE ENGINEER"),
              _opt(2, "Person  Abhijit Kumar", "Abhijit Kumar\n\nLEAD"),
              _opt(3, "Person  Roshan Kumar Thakur", "Roshan Kumar Thakur\n\nQA")]
    with pytest.raises(RuntimeError) as exc:
        teams_bridge._one_of(people, "Kumar", "people")
    msg = str(exc.value)
    assert "matches 3 people" in msg
    assert "Abhijit Kumar" in msg and "Vinish Kumar" in msg
    assert "which one he means" in msg


def test_an_exact_name_wins_over_a_longer_one_that_also_matches():
    """Otherwise having a "Vinish Kumar Singh" in the directory would make
    "Vinish Kumar" permanently unsendable."""
    people = [_opt(1, "Person  Vinish Kumar", "Vinish Kumar"),
              _opt(2, "Person  Vinish Kumar Singh", "Vinish Kumar Singh")]
    assert teams_bridge._one_of(people, "Vinish Kumar", "people")["i"] == 1


def test_the_same_person_listed_twice_is_one_candidate():
    """Teams lists a person as a chat result AND a directory hit. Three rows for
    one human must not read as an ambiguity that blocks the send."""
    rows = [_opt(1, "Person  Vinish Kumar", "Vinish Kumar\n\nSOFTWARE ENGINEER"),
            _opt(4, "Person  Vinish Kumar", "Vinish Kumar\n\n(VINISH.KUMAR)")]
    assert len(teams_bridge._dedupe(rows)) == 1


def test_ambiguous_groups_are_refused_the_same_way():
    groups = [_opt(1, "Group chat  prod issue - triaging", "prod issue - triaging"),
              _opt(2, "Group chat  prod issue - escalations", "prod issue - escalations")]
    with pytest.raises(RuntimeError, match="matches 2 groups"):
        teams_bridge._one_of(groups, "prod issue", "groups")


def test_the_display_name_ignores_the_role_line_teams_appends():
    assert teams_bridge._display_name(
        _opt(1, "", "Vinish Kumar\n\n(VINISH.KUMAR) SOFTWARE ENGINEER")) == "Vinish Kumar"


# --- who he actually talks to beats who exists in the directory -------------
#
# Arun's own argument, and it is the right one: a half-name is nearly always
# somebody he is already in a conversation with, because when he means a stranger
# he types the full name. Ranking the directory was solving the rare case at the
# expense of the common one.

def _p(i, name, top=False, chat=False):
    return {"i": i, "aria": f"Person  {name}", "text": name,
            "tid": ("AUTOSUGGEST_SUGGESTION_TOPHITS8" if top
                    else "AUTOSUGGEST_SUGGESTION_PEOPLE8"), "_chat": chat}


def test_someone_he_is_already_talking_to_wins_over_the_directory():
    """Two Surajs are top hits. One is in his rail. That settles it."""
    rows = [_p(1, "Suraj Prakash", top=True), _p(2, "Suraj Shaikh", top=True)]
    picked = teams_bridge._one_of(rows, "Suraj", "people", {"suraj prakash"})
    assert teams_bridge._display_name(picked) == "Suraj Prakash"


def test_a_shared_surname_resolves_to_the_one_he_has_a_chat_with():
    """"Kumar" matches five people in the directory and one in his rail."""
    rows = [_p(1, "Vinish Kumar", top=True), _p(2, "Roshan Kumar Thakur", top=True),
            _p(3, "Rajendra Kumar"), _p(4, "Yogesh Kumar Singh")]
    picked = teams_bridge._one_of(rows, "Kumar", "people", {"vinish kumar"})
    assert teams_bridge._display_name(picked) == "Vinish Kumar"


def test_two_people_he_talks_to_is_still_a_refusal_and_says_why():
    """The rail cannot break a tie between two people who are both on it. The
    message names that specifically, so the reason is legible."""
    rows = [_p(1, "Suraj Prakash", top=True), _p(2, "Suraj Shaikh", top=True)]
    with pytest.raises(RuntimeError) as exc:
        teams_bridge._one_of(rows, "Suraj", "people", {"suraj prakash", "suraj shaikh"})
    assert "open chats with both" in str(exc.value)


def test_a_stranger_is_reachable_by_full_name():
    """His stated workflow: new person, full name. That must not be blocked by
    everyone he DOES talk to ranking above them."""
    rows = [_p(1, "Nakka Harika", top=True, chat=True), _p(2, "Harika Reddy")]
    picked = teams_bridge._one_of(rows, "Harika Reddy", "people", {"nakka harika"})
    assert teams_bridge._display_name(picked) == "Harika Reddy"


def test_the_exact_name_still_outranks_the_rail():
    """Otherwise having chatted with Vinish Kumar would make "Vinish Kumar Balaji"
    permanently unreachable even when spelled out in full."""
    rows = [_p(1, "Vinish Kumar", top=True), _p(2, "Vinish Kumar Balaji")]
    picked = teams_bridge._one_of(rows, "Vinish Kumar Balaji", "people", {"vinish kumar"})
    assert teams_bridge._display_name(picked) == "Vinish Kumar Balaji"


def test_an_empty_rail_falls_back_to_the_directory_ranking():
    """First run on a new machine, or a rail that would not read. Degrades to the
    previous behaviour rather than refusing everything."""
    rows = [_p(1, "Vinish Kumar", top=True), _p(2, "Vinish Kumar Balaji")]
    assert teams_bridge._display_name(
        teams_bridge._one_of(rows, "Vinish", "people", set())) == "Vinish Kumar"


class _RailPage:
    def __init__(self, rows, boom=False):
        self.rows, self.boom = rows, boom

    async def evaluate(self, script, *a):
        if self.boom:
            raise RuntimeError("rail not rendered")
        return self.rows


def test_the_rail_drops_navigation_furniture():
    rows = ["Copilot", "Mentions", "Drafts", "Saved", "Vinish Kumar", "prod issue - triaging"]
    got = asyncio.run(teams_bridge.recent_chats(_RailPage(rows)))
    assert "vinish kumar" in got and "prod issue - triaging" in got
    assert "copilot" not in got and "drafts" not in got


def test_the_rail_is_remembered_because_teams_only_renders_part_of_it():
    """The bug this exists for: the rail is virtualised, so one poll saw Suraj
    Prakash and the next did not — the same question answered two ways with
    nothing changed but scroll position."""
    asyncio.run(teams_bridge.recent_chats(_RailPage(["Suraj Prakash", "Vinish Kumar"])))
    later = asyncio.run(teams_bridge.recent_chats(_RailPage(["Vinish Kumar"])))
    assert "suraj prakash" in later, "a name seen once must not vanish on the next poll"


def test_a_rail_that_cannot_be_read_still_returns_what_was_remembered():
    asyncio.run(teams_bridge.recent_chats(_RailPage(["Vinish Kumar"])))
    assert "vinish kumar" in asyncio.run(teams_bridge.recent_chats(_RailPage([], boom=True)))


def test_the_remembered_rail_is_capped():
    big = [f"Person Number {n}" for n in range(teams_bridge._RAIL_MAX + 120)]
    got = asyncio.run(teams_bridge.recent_chats(_RailPage(big)))
    assert len(got) <= teams_bridge._RAIL_MAX


# --- the header must catch up before it is trusted --------------------------

def test_a_thread_title_matches_on_any_token_he_used():
    assert teams_bridge._title_matches("Vinish Kumar", "vinish")
    assert teams_bridge._title_matches("Daily deployment slot - evening", "daily deployment slot")


def test_a_different_persons_thread_never_counts_as_a_match():
    assert not teams_bridge._title_matches("Suraj Prakash", "kavitha")


def test_the_code_waits_for_the_header_instead_of_sleeping():
    """The race this replaces: the post-click wait was satisfied by whichever
    conversation was ALREADY open, so the title read back the previous chat and
    the guard reported opening the wrong thread. The guard was right; the wait
    under it was the bug."""
    import inspect
    src = inspect.getsource(teams_bridge._find_chat)
    assert "before = await _chat_title(page)" in src
    assert "title != before" in src


# --- the approved words are the words that go -------------------------------

def test_an_approved_teams_draft_becomes_a_recorded_call():
    staged = {"channel": "teams", "to": "Vinish Kumar",
              "what": "Deployed the fix to SIT — please retest when you get a minute."}
    op = main._mechanical_send(staged)
    assert op == {"name": "teams_send",
                  "args": {"to": "Vinish Kumar",
                           "text": "Deployed the fix to SIT — please retest when you "
                                   "get a minute.",
                           "to_group": False}}


def test_the_text_is_carried_verbatim_not_summarised():
    exact = "1) rebased\n2) crowdstrike fix in\n3) needs your approve on #1330"
    op = main._mechanical_send({"channel": "teams", "to": "Harika", "what": exact})
    assert op["args"]["text"] == exact


def test_a_group_send_stays_a_group_send_through_staging():
    op = main._mechanical_send({"channel": "teams", "to": "prod issue - triaging",
                                "what": "SIT is back up.", "to_group": True})
    assert op["args"]["to_group"] is True


def test_a_teams_draft_with_no_recipient_still_goes_back_to_the_model():
    """Nothing to address it to is not something to guess at."""
    assert main._mechanical_send({"channel": "teams", "to": "", "what": "hi"}) is None


def test_email_and_pr_drafts_are_left_on_the_path_they_were_already_on():
    """Nothing here composes an email. Claiming those mechanically would break
    them to fix a problem they do not have."""
    for ch in ("email", "pr", "chat"):
        assert main._mechanical_send({"channel": ch, "to": "x", "what": "y"}) is None


def test_the_send_op_is_registered_and_runs_the_real_bridge(monkeypatch):
    sent = {}

    async def fake_send(chat, text, allow_group=False):
        sent.update(chat=chat, text=text, allow_group=allow_group)
        return "Vinish Kumar"

    monkeypatch.setattr(teams_bridge, "send_message", fake_send)
    line = asyncio.run(ops.run({"name": "teams_send",
                                "args": {"to": "Vinish", "text": "ping", "to_group": False}}))
    assert sent == {"chat": "Vinish", "text": "ping", "allow_group": False}
    assert "Sent to Vinish Kumar" in line


def test_a_failed_send_is_not_reported_as_success(monkeypatch):
    """The bridge verifies delivery by reading the thread back. That verdict has
    to survive up to Arun instead of being flattened into a cheerful line."""
    async def refuses(chat, text, allow_group=False):
        raise RuntimeError("opened 'Vinish Kumar Singh' instead of 'Vinish' — aborted")

    monkeypatch.setattr(teams_bridge, "send_message", refuses)
    with pytest.raises(RuntimeError, match="aborted"):
        asyncio.run(ops.run({"name": "teams_send", "args": {"to": "Vinish", "text": "x"}}))


def test_the_offer_names_the_group_so_he_sees_it_before_saying_yes():
    describe = ops.REGISTRY["teams_send"]["describe"]
    assert "GROUP" in describe({"to": "prod issue - triaging", "to_group": True})
    assert "GROUP" not in describe({"to": "Vinish", "to_group": False})


# --- staging carries the group flag -----------------------------------------

def test_staging_records_whether_the_target_is_a_group():
    loop.set_pending_send("c1", "SIT is back up", "prod issue - triaging", "teams",
                          to_group=True)
    assert loop.take("c1")["to_group"] is True


def test_staging_defaults_to_a_person():
    """"ping X" means X, and a default that could mean fourteen people is not a
    default anybody should have to remember to override."""
    loop.set_pending_send("c2", "hi", "Vinish", "teams")
    assert loop.take("c2")["to_group"] is False


# --- calling ----------------------------------------------------------------

def test_calling_is_staged_and_nothing_rings(monkeypatch):
    from app import agent, offers
    offers.clear()
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    monkeypatch.setattr("app.meetings.call_person",
                        lambda *a, **k: pytest.fail("nothing may ring before he says yes"))
    out = asyncio.run(agent.teams_call("Vinish"))
    assert "waiting for Arun's yes" in out
    assert offers.pending().op == {"name": "teams_call",
                                   "args": {"who": "Vinish", "video": False}}


def test_a_call_that_never_connected_is_reported_as_not_called(monkeypatch):
    async def never_connects(who, video=False):
        raise RuntimeError(f"clicked audio call for '{who}' but no call ever started "
                           f"— treat as NOT called")

    monkeypatch.setattr("app.meetings.call_person", never_connects)
    with pytest.raises(RuntimeError, match="NOT called"):
        asyncio.run(ops.run({"name": "teams_call", "args": {"who": "Vinish"}}))
