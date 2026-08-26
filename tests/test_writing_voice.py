"""Drafts that read like Arun, and links that survive being sent.

Both faults come from one real message he shared, sent to Vinish:

    Raised the fix for BEPTELIKOS-10159 — PR #1371: https://…/pull/1371.
    CANCELLED bookings (regardless of timeout flag) and bookings with
    SERVICE_DELIVERY_EXECUTION already EXECUTED now short-circuit before the
    vessel/rail date update and SEND_TO_TMS trigger. 17/17 tests pass. Please
    review when you get a chance.

The full stop welded to the URL is why it did not render as a link. The rest is
why he said it "easily looks like a bot" — three hundred characters of release
note from someone whose own median message in that same chat is under thirty.
"""

from __future__ import annotations

import pytest

from app import store, writing

REAL_MESSAGE = (
    "Raised the fix for BEPTELIKOS-10159 — PR #1371: "
    "https://github.com/Maersk-Global/telikos-booking-service/pull/1371. "
    "CANCELLED bookings now short-circuit. 17/17 tests pass. "
    "Please review when you get a chance."
)
PR_URL = "https://github.com/Maersk-Global/telikos-booking-service/pull/1371"


# --- links --------------------------------------------------------------------

def test_the_full_stop_is_taken_off_the_url():
    """The exact defect in the screenshot."""
    out = writing.tidy_links(REAL_MESSAGE)
    assert f"{PR_URL}." not in out
    assert PR_URL in out


def test_the_url_ends_up_alone_on_its_line():
    """Nothing adjacent means nothing that can be swallowed into the href."""
    out = writing.tidy_links(REAL_MESSAGE)
    assert any(line.strip() == PR_URL for line in out.splitlines()), out


def test_the_words_around_the_url_survive():
    out = writing.tidy_links(REAL_MESSAGE)
    assert "BEPTELIKOS-10159" in out
    assert "17/17 tests pass" in out


def test_a_url_already_on_its_own_line_is_left_alone():
    text = f"raised the PR bro\n{PR_URL}\nreview once"
    assert writing.tidy_links(text) == text


def test_a_trailing_comma_or_question_mark_goes_too():
    for punct in (",", "?", "!", ";", ":"):
        out = writing.tidy_links(f"see {PR_URL}{punct}")
        assert f"{PR_URL}{punct}" not in out
        assert PR_URL in out


def test_a_url_with_a_query_string_is_not_truncated():
    url = "https://github.com/o/r/pull/7?tab=files"
    out = writing.tidy_links(f"check {url}.")
    assert url in out


def test_a_message_with_no_url_is_untouched():
    text = "all merged bro"
    assert writing.tidy_links(text) == text


def test_two_urls_both_get_their_own_line():
    a, b = "https://github.com/o/r/pull/7", "https://github.com/o/e/pull/3"
    out = writing.tidy_links(f"booking {a}. email {b}.")
    lines = [line.strip() for line in out.splitlines()]
    assert a in lines and b in lines


def test_empty_input_is_safe():
    assert writing.tidy_links("") == ""
    assert writing.tidy_links(None) is None


# --- his voice ----------------------------------------------------------------

def _seed(texts, sender="Arunkumar K"):
    store.save_teams_messages([
        {"key": f"k{i}-{sender}", "chat": "Vinish Kumar", "sender": sender,
         "text": t, "sent_at": 1_786_000_000.0 + i, "stamp": ""}
        for i, t in enumerate(texts)])


#: Real messages of his, taken from the stored history.
HIS = ["all merged bro", "go ahead and create new release", "on top of develop",
       "call me bro", "bro review the current code once", "when it happened",
       "no duplicate na", "Done bro", "i need to discuss with u once",
       "we can merge and create new release", "in the same PR", "Sure",
       "added PR description also how it helps", "bro last deploymwntn",
       "i told in group also", "few milli secs diff", "same order duplicate wont be possible right",
       "once u dropped", "I also put on hold", "Anything for me",
       "what was the issue bro", "Bro all sorted ?", "inform there and assign me na"]


def test_no_profile_without_enough_of_his_writing():
    """Four messages cannot describe a style, and inventing one impersonates him."""
    _seed(HIS[:4])
    assert writing.profile() == {}
    assert writing.guidance() == ""


def test_the_profile_measures_how_short_he_is():
    _seed(HIS)
    p = writing.profile()
    assert p["samples"] >= 20
    assert p["median_chars"] < 40, "the whole point is that he writes very short"


def test_his_prose_tics_are_picked_up():
    _seed(HIS)
    tics = writing.profile()["tics"]
    assert any(t in tics for t in ("na", "u", "ur", "once"))


def test_a_term_of_address_is_never_a_global_tic():
    """It travels with the RELATIONSHIP, not with him.

    Pooling made "bro" look like part of his voice, so it would have been
    offered for every recipient — which is the thing he caught. Terms of address
    are per-person and live in profile(chat=…)["address"]; see
    test_writing_address.py.
    """
    _seed(HIS)
    assert "bro" not in writing.profile()["tics"]


def test_lowercase_starts_are_noticed():
    _seed(HIS)
    assert writing.profile()["lowercase_start_pct"] > 50


def test_messages_from_other_people_are_not_counted_as_his():
    """Otherwise the 'voice' is the average of the chat, not of Arun."""
    _seed(HIS)
    _seed(["Bro, assigned this defect to you. Please update the team and story points."] * 30,
          sender="Vinish Kumar")
    p = writing.profile()
    assert p["samples"] == len(HIS)
    assert p["median_chars"] < 40, "another person's long messages leaked in"


def test_reaction_rows_are_not_treated_as_prose():
    _seed(HIS + ["1 Laugh reaction.", "1 Like reaction with medium light skin tone."])
    assert all("reaction" not in e.lower() for e in writing.profile()["examples"])


def test_the_guidance_names_the_exact_phrase_he_objected_to():
    _seed(HIS)
    g = writing.guidance()
    assert "Please review when you get a chance" in g
    assert "measured" in g.lower()


def test_the_guidance_shows_real_examples_not_descriptions():
    """A model told "write short" still writes short corporate English.

    Every example must be something he actually sent — a paraphrase would be me
    describing him, which is the guess this module exists to avoid.
    """
    _seed(HIS)
    examples = writing.profile()["examples"]
    assert examples, "no examples offered at all"
    assert all(e in HIS for e in examples), examples
    g = writing.guidance()
    assert all(e in g for e in examples)


def test_the_guidance_says_a_url_needs_its_own_line():
    _seed(HIS)
    assert "own line" in writing.guidance()


def test_chat_voice_is_not_applied_to_a_pr_or_jira_body():
    """"bro" belongs in a DM, never in something his whole team reads."""
    _seed(HIS)
    g = writing.guidance()
    assert "Jira" in g and "PR body" in g
    assert "chat only" in g.lower()


def test_the_profile_follows_him_rather_than_being_hardcoded():
    """If he starts writing differently, the guidance changes with him."""
    _seed(["I will review this and revert with detailed comments shortly."] * 25)
    p = writing.profile()
    assert p["median_chars"] > 40
    assert "bro" not in p["tics"]


# --- staging ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_staging_a_draft_repairs_the_link_before_arun_sees_it(monkeypatch):
    """He approves what will actually be sent, so the repair has to happen first."""
    from app import agent, loop, tasks

    monkeypatch.setattr(tasks, "current_conversation", lambda: "conv-1")
    staged = {}
    monkeypatch.setattr(loop, "set_pending_send",
                        lambda cid, what, to, channel, to_group=False:
                        staged.update(what=what, to=to))

    agent.prepare_to_send(REAL_MESSAGE, to="Vinish", channel="teams")
    assert f"{PR_URL}." not in staged["what"]
    assert any(line.strip() == PR_URL for line in staged["what"].splitlines())
