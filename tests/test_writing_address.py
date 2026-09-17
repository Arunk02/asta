"""Who Arun is allowed to call what.

He asked the question that broke the first version of this: "bro is for boys,
how will it send for girls".

The tempting fix is to decide, per recipient, whether "bro" suits them — which
means inferring someone's gender from their name. That is wrong often, it is not
something to automate, and it still would not answer the real question, because
plenty of men in his contacts have never been called "bro" either.

So the rule here is evidence instead of inference: **a term of address is used
with a person only if he has used it with that person.** In his stored history
"bro" appears 35 times and every single one is in one chat, with Alex. For
everybody else — any gender, any seniority — there is no evidence, so there is
no term.

Two layers, both tested below: the prompt tells the model, and `fit_address`
enforces it mechanically on the way to staging, because a model that has been
told a rule still slips and the cost of slipping is a message he would have to
apologise for rather than simply correct.
"""

from __future__ import annotations

import pytest

from app import agent, loop, store, tasks, writing

ALEX = "Alex Kumar"


def _seed(chat, texts, sender="Arunkumar K"):
    store.save_teams_messages([
        {"key": f"{chat}-{sender}-{i}-{hash(t) & 0xffff}", "chat": chat,
         "sender": sender, "text": t, "sent_at": 1_786_000_000.0 + i, "stamp": ""}
        for i, t in enumerate(texts)])


#: Real messages of his to Alex, "bro" and all.
TO_ALEX = ["all merged bro", "call me bro", "bro review the current code once",
             "Done bro", "what was the issue bro", "Bro all sorted ?",
             "go ahead and create new release", "on top of develop",
             "no duplicate na", "i need to discuss with u once",
             "in the same PR", "we can merge and create new release",
             "added PR description also how it helps", "once u dropped",
             "I also put on hold", "Anything for me", "few milli secs diff",
             "i told in group also", "inform there and assign me na",
             "same order duplicate wont be possible right", "Sure", "when it happened"]

#: A colleague he has messaged, but never with a term of address.
TO_ELLIS = ["can you check the deployment once", "the ETA is not coming through",
              "shared the logs in the group", "will raise the PR today"]


@pytest.fixture(autouse=True)
def _history():
    _seed(ALEX, TO_ALEX)


# --- what he is attested to use ----------------------------------------------

def test_bro_is_attested_for_alex():
    assert "bro" in writing.address_terms(ALEX)


def test_nothing_is_attested_for_someone_he_has_never_addressed():
    _seed("Ellis", TO_ELLIS)
    assert writing.address_terms("Ellis") == []


def test_nothing_is_attested_for_someone_with_no_history_at_all():
    assert writing.address_terms("Priya Menon") == []


def test_an_empty_recipient_yields_nothing_rather_than_everything():
    assert writing.address_terms("") == []


def test_a_common_word_is_not_learned_as_a_term_of_address():
    """"all merged" and "all sorted" must not make "all" a way he addresses Alex."""
    assert "all" not in writing.address_terms(ALEX)


def test_the_other_persons_words_are_not_learned_as_his():
    """Alex calls HIM "bro"; that is not evidence about what Arun calls anyone."""
    _seed("Kendall", ["Bro, please check this", "bro any update"], sender="Kendall K")
    assert writing.address_terms("Kendall") == []


# --- the mechanical backstop --------------------------------------------------

@pytest.mark.parametrize("draft,expected", [
    ("all merged bro", "all merged"),
    ("bro review the current code once", "review the current code once"),
    ("Done bro.", "Done."),
    ("Bro all sorted ?", "all sorted ?"),
    ("call me bro", "call me"),
    # Vocative but not at either end. The greeting stays — removing the term is
    # this function's job, rewriting his prose is not.
    ("hey dude can you check", "hey can you check"),
    ("ok bro will do", "ok will do"),
    ("thanks mate", "thanks"),
    ("sir please review", "please review"),
    ("madam the build is red", "the build is red"),
])
def test_terms_he_has_not_used_with_someone_are_removed(draft, expected):
    assert writing.fit_address(draft, "Ellis") == expected


def test_the_attested_term_survives_for_the_person_it_belongs_to():
    for draft in ("all merged bro", "call me bro", "Done bro."):
        assert "bro" in writing.fit_address(draft, ALEX)


def test_the_attested_term_does_not_leak_to_anybody_else():
    """The whole question he asked."""
    assert "bro" not in writing.fit_address("all merged bro", "Ellis").lower()
    assert "bro" not in writing.fit_address("all merged bro", "Priya Menon").lower()


# --- edge cases: words that merely contain a term -----------------------------

@pytest.mark.parametrize("draft", [
    "my brother is visiting",
    "broadcast the change to the team",
    "the brochure is attached",
    "check the embargo date",
    "the man page says otherwise",
    "broker config is wrong",
])
def test_an_ordinary_word_is_never_mutilated(draft):
    assert writing.fit_address(draft, "Ellis") == draft


def test_a_url_containing_a_term_is_untouched():
    """Editing inside a link is how a working URL becomes a 404."""
    url = "https://github.com/bro/repo/pull/1"
    assert url in writing.fit_address(f"see {url}", "Ellis")


def test_a_url_that_is_the_whole_message_is_untouched():
    url = "https://github.com/Maersk-Global/telikos-booking-service/pull/1371"
    assert writing.fit_address(url, "Ellis") == url


def test_a_name_that_looks_like_a_term_is_left_alone():
    """"Anna" is a person. Cutting it out of a sentence is worse than leaving it."""
    assert writing.fit_address("Anna will check it", "Ellis") == "Anna will check it"


def test_a_term_mid_sentence_is_left_alone():
    """Mid-sentence it is usually a real word, and rewriting prose is not the job."""
    got = writing.fit_address("the buddy system works here", "Ellis")
    assert got == "the buddy system works here"


# --- edge cases: shape of the message -----------------------------------------

def test_a_message_that_is_only_the_term_is_not_emptied():
    """Sending nothing is a worse failure than sending the wrong word."""
    assert writing.fit_address("bro", "Ellis") == "bro"


def test_empty_and_none_are_safe():
    assert writing.fit_address("", "Ellis") == ""
    assert writing.fit_address(None, "Ellis") is None


def test_every_line_of_a_multiline_draft_is_checked():
    draft = "bro the build is red\nhttps://github.com/o/r/pull/7\nfix it mate"
    got = writing.fit_address(draft, "Ellis")
    assert got.splitlines()[0] == "the build is red"
    assert got.splitlines()[2] == "fix it"
    assert "https://github.com/o/r/pull/7" in got


def test_two_terms_in_one_line_both_go():
    assert writing.fit_address("bro check this mate", "Ellis") == "check this"


def test_a_comma_fenced_term_goes_with_its_comma():
    assert writing.fit_address("ok, bro, will do", "Ellis") == "ok, will do"


def test_no_double_spaces_are_left_behind():
    assert "  " not in writing.fit_address("bro  check this", "Ellis")


def test_capitalisation_of_the_term_does_not_matter():
    for form in ("bro", "Bro", "BRO"):
        assert "bro" not in writing.fit_address(f"{form} check this", "Ellis").lower()


# --- what the model is told ---------------------------------------------------

def test_the_prompt_permits_the_term_for_the_person_it_belongs_to():
    g = writing.guidance(ALEX)
    assert "bro" in g
    assert "attested" in g
    assert "Do not carry it to anyone else" in g


def test_the_prompt_forbids_a_term_for_anyone_else():
    g = writing.guidance("Ellis")
    assert "NO term of address for Ellis" in g
    assert "attested" not in g.split("NO term")[1][:200]


def test_the_default_prompt_with_no_recipient_forbids_a_term():
    """Assembled before the recipient is known, so it must default to safe."""
    g = writing.guidance()
    assert "NO term of address" in g


def test_the_prompt_never_reasons_about_gender():
    """The fix is evidence, not a guess about who a word suits."""
    for g in (writing.guidance(ALEX), writing.guidance("Ellis"), writing.guidance()):
        low = g.lower()
        for word in ("male", "female", "girl", "boy", "gender", "man or woman"):
            assert word not in low, f"{word!r} leaked into the guidance"


# --- end to end, through staging ----------------------------------------------

@pytest.fixture
def staged(monkeypatch):
    monkeypatch.setattr(tasks, "current_conversation", lambda: "conv-1")
    box = {}
    monkeypatch.setattr(loop, "set_pending_send",
                        lambda cid, what, to, channel, to_group=False:
                        box.update(what=what, to=to, channel=channel, group=to_group))
    return box


def test_staging_to_a_new_colleague_strips_the_term(staged):
    agent.prepare_to_send("all merged bro", to="Ellis", channel="teams")
    assert staged["what"] == "all merged"


def test_staging_to_alex_keeps_it(staged):
    agent.prepare_to_send("all merged bro", to=ALEX, channel="teams")
    assert staged["what"] == "all merged bro"


def test_a_jira_comment_is_not_rewritten(staged):
    """A ticket has no term of address to get wrong, and it is read by the team."""
    agent.prepare_to_send("all merged bro", to="BEPTELIKOS-10159", channel="jira")
    assert staged["what"] == "all merged bro"


def test_a_pr_body_is_not_rewritten(staged):
    agent.prepare_to_send("fixes the man page link", to="pr", channel="pr")
    assert staged["what"] == "fixes the man page link"


def test_links_and_address_are_both_fixed_in_one_pass(staged):
    """The two faults he reported, in the same draft."""
    agent.prepare_to_send(
        "raised it bro: https://github.com/o/r/pull/1371. please review",
        to="Ellis", channel="teams")
    out = staged["what"]
    assert "bro" not in out.lower()
    assert "https://github.com/o/r/pull/1371." not in out
    assert any(l.strip() == "https://github.com/o/r/pull/1371" for l in out.splitlines())


def test_a_group_send_is_still_checked(staged):
    """A group is where using the wrong word is seen by the most people."""
    agent.prepare_to_send("all merged bro", to="BEP Telikos : Defect Triage",
                          channel="teams", to_group=True)
    assert "bro" not in staged["what"].lower()


def test_the_voice_tool_answers_for_a_specific_person():
    assert "bro" in agent.draft_voice(ALEX)
    assert "NO term of address" in agent.draft_voice("Ellis")


def test_the_voice_tool_is_honest_when_there_is_no_history(monkeypatch):
    monkeypatch.setattr(writing, "guidance", lambda chat="": "")
    out = agent.draft_voice("Someone New")
    assert "NO term of address" in out
