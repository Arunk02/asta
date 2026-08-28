"""Going and finding out, instead of only telling him someone asked.

Arun, on what was missing:

    "vinish asked ... to check the production temporal bookings struck but thee
     doesnt seen or not i'm not aware ... same for pr 1049 when he shared the
     message it should known automatically do the analysis to check right whether
     it is true or wrong ... it should always on someone pings anything whether im
     online or not ... that provactiveness is missing"

The inbound pipeline ended at `notify`. Everything in it was a sensor. These tests
pin the actuator, and — more importantly — the three limits that keep an actuator
from becoming a liability: it only ever READS, it never consults presence, and it
is bounded so ten people pinging does not become ten investigations.
"""

from __future__ import annotations

import pytest

from app import attention, responder, store

#: Vinish's actual question, near enough.
TEMPORAL = "can you check the production temporal bookings struck or not"
PR_FEEDBACK = "I left some comments on PR 1049, can you check whether they are valid"
DEBUG = "can you look into why the booking activity is failing"
CHATTER = "good morning team, standup in 5"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ASTA_RESPOND", "1")
    store.kv_set(responder._RATE_KEY, "")
    store.kv_set(responder._DONE_KEY, "")


@pytest.fixture
def known_ground(monkeypatch):
    """Treat the ask as a continuation of work Asta has already done.

    Most tests here are about something else — the rate limit, the brief, the
    title — and since `respond` began OFFERING new ground instead of starting it,
    an isolated store (which has no task history) turns every one of them into an
    offer. Stating the assumption is better than each test quietly depending on
    what happens to be in the tasks table.
    """
    monkeypatch.setattr(responder, "familiar", lambda text: (True, "test: known"))


@pytest.fixture
def spawned(monkeypatch):
    """Capture what would be delegated, without running a worker."""
    calls = []

    def _fake(title, prompt, kind="analysis", workspace=None, *a, **k):
        calls.append({"title": title, "prompt": prompt, "kind": kind,
                      "workspace": workspace})
        return {"id": 100 + len(calls)}

    from app import tasks
    monkeypatch.setattr(tasks, "spawn", _fake)
    return calls


# --- the three things he named ------------------------------------------------

def test_a_production_question_is_investigated(spawned, known_ground):
    """His example. Vinish asks whether prod bookings are stuck; Asta goes and
    looks instead of forwarding the question to a man who is not at his desk."""
    t = responder.respond("teams", "Vinish", TEMPORAL, priority=attention.P_TODAY)
    assert t, "nothing was investigated"
    assert spawned[0]["kind"] == "analysis"
    assert "Temporal" in spawned[0]["prompt"]


def test_pr_feedback_is_verified_rather_than_believed(spawned, known_ground):
    """"check right whether it is true or wrong". The reviewer is not assumed
    correct — that assumption is what pushed seven unreviewed edits on 27 August."""
    responder.respond("teams", "Vinish", PR_FEEDBACK, priority=attention.P_TODAY)
    prompt = spawned[0]["prompt"]
    assert "1049" in prompt
    assert "Do not assume the reviewer is right" in prompt
    assert "do not change any code" in prompt.lower()


def test_a_debug_request_is_picked_up(spawned, known_ground):
    responder.respond("teams", "Vinish", DEBUG, priority=attention.P_TODAY)
    assert spawned and spawned[0]["kind"] == "analysis"


def test_ordinary_chatter_starts_nothing(spawned):
    assert responder.respond("teams", "Vinish", CHATTER, priority=attention.P_TODAY) is None
    assert not spawned


@pytest.mark.parametrize("text,kind", [
    ("prod bookings are stuck since morning", "incident"),
    ("temporal workflows failing in production", "incident"),
    ("there is an incident on the booking service", "incident"),
    ("please review my feedback on pull request 1409", "pr_review"),
    ("any idea why the build is red", "debug"),
    ("please check the logs for this error", "debug"),
    ("lunch at 1?", ""),
    ("thanks, that worked", ""),
])
def test_what_it_asks(text, kind):
    assert responder.what_it_asks(text) == kind, text


def test_a_production_incident_outranks_a_pr_mention():
    """"prod is down, see PR 1409" is an outage, not a review request. Being wrong
    the other way tells him about an outage in the wrong words."""
    assert responder.what_it_asks(
        "prod bookings are stuck, might be from PR 1409") == "incident"


# --- the limits that make an actuator safe ------------------------------------

def test_it_can_only_ever_read(spawned, known_ground):
    """The whole design rests on this. A code task edits a repository and pushes;
    an analysis task reads and reports. Nothing here may spawn the former."""
    for text in (TEMPORAL, PR_FEEDBACK, DEBUG):
        store.kv_set(responder._DONE_KEY, "")
        store.kv_set(responder._RATE_KEY, "")
        responder.respond("teams", "Vinish", text, priority=attention.P_NOW)
    assert spawned
    assert {c["kind"] for c in spawned} == {"analysis"}


def test_it_never_sends_anything_itself(spawned, known_ground):
    """It may draft. Staging is his gate, and the brief must say so every time —
    a worker told only "reply to Vinish" is a worker that replies to Vinish."""
    for text in (TEMPORAL, PR_FEEDBACK, DEBUG):
        store.kv_set(responder._DONE_KEY, "")
        store.kv_set(responder._RATE_KEY, "")
        responder.respond("teams", "Vinish", text, priority=attention.P_NOW)
    for call in spawned:
        assert "prepare_to_send" in call["prompt"]
        assert "never send anything yourself" in call["prompt"]


def test_presence_is_never_consulted():
    """"it should always on someone pings anything whether im online or not."
    Presence decides how loudly he is told; it must not decide whether Asta works.

    Asserted on the parsed module rather than its text, because the failure here
    is a call that is NOT made — there is no runtime moment to observe — and the
    first version of this test matched the word in its own docstring."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(responder))
    imported = {n.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) for n in node.names}
    imported |= {a.name.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for a in node.names}
    assert "presence" not in imported, "the responder imports presence — it must not"
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "at_laptop" not in attrs


def test_the_brief_is_self_contained(spawned, known_ground):
    """The worker has no chat context — `delegate_task` says so. A prompt that
    says "the message above" reaches a worker that cannot see any message."""
    responder.respond("teams", "Vinish", TEMPORAL, priority=attention.P_TODAY)
    prompt = spawned[0]["prompt"]
    assert TEMPORAL in prompt, "the message itself is not in the brief"
    assert "the above" not in prompt.lower()
    assert "Vinish" in prompt


def test_ten_people_pinging_is_not_ten_investigations(spawned, known_ground, monkeypatch):
    """His burst scenario, and the reason a rate limit is not optional: each of
    these is a full agentic turn against production systems."""
    monkeypatch.setattr(responder, "MAX_PER_HOUR", 3)
    for i in range(10):
        responder.respond("teams", f"Person{i}", f"{TEMPORAL} number {i}",
                          priority=attention.P_TODAY)
    assert len(spawned) == 3


def test_the_same_ask_is_investigated_once(spawned, known_ground):
    """A caption or activity row settling over several polls arrives repeatedly."""
    for _ in range(4):
        responder.respond("teams", "Vinish", TEMPORAL, priority=attention.P_TODAY,
                          key="same-key")
    assert len(spawned) == 1


def test_low_priority_noise_is_not_worth_a_turn(spawned):
    """P_FYI is something he was copied on. Spending an investigation on each is
    the noise he already complained about wearing a different hat."""
    assert responder.respond("teams", "Vinish", TEMPORAL,
                             priority=attention.P_MUTE) is None
    assert not spawned


def test_it_is_off_until_switched_on(monkeypatch, spawned):
    """Same as every other behaviour that spends money on his behalf."""
    monkeypatch.delenv("ASTA_RESPOND", raising=False)
    assert responder.respond("teams", "Vinish", TEMPORAL,
                             priority=attention.P_NOW) is None
    assert not spawned


def test_every_refusal_has_a_stated_reason(monkeypatch):
    """"why didn't you check that one" must have an answer, and it is this."""
    monkeypatch.delenv("ASTA_RESPOND", raising=False)
    assert "off" in responder.should_respond("incident", 0, "k1")
    monkeypatch.setenv("ASTA_RESPOND", "1")
    assert responder.should_respond("", 0, "k2") == "nothing checkable in it"
    assert "below the bar" in responder.should_respond("incident", attention.P_MUTE, "k3")
    assert responder.should_respond("incident", 0, "k4") == ""


# --- both sources, one behaviour ----------------------------------------------

def test_teams_and_outlook_both_call_it():
    """A responder wired into one source and not the other is the per-source drift
    that lost `critical` on Teams and the L2 exemption on mail."""
    import inspect

    from app import outlook, teams_bridge
    for mod in (teams_bridge, outlook):
        src = inspect.getsource(mod)
        assert "responder.respond(" in src, mod.__name__


def test_he_is_told_the_checking_has_started():
    """"hey X person asking for bug issue, can i analyse and move forward" — the
    line has to name the person and say it is already running."""
    line = responder.line_for({"id": 42}, "Vinish", "incident")
    assert "Vinish" in line
    assert "#42" in line
    assert "checking" in line.lower()


def test_the_answer_arrives_still_attached_to_its_question(spawned, known_ground):
    """A finished analysis pushes "✅ Task #N done — <title>", possibly hours later
    and out of any context. A title that omits who asked hands him an answer with
    no question attached."""
    for text in (TEMPORAL, PR_FEEDBACK, DEBUG):
        store.kv_set(responder._DONE_KEY, "")
        store.kv_set(responder._RATE_KEY, "")
        responder.respond("teams", "Vinish", text, priority=attention.P_NOW)
    assert spawned
    for call in spawned:
        assert "Vinish" in call["title"], call["title"]


def test_a_pr_title_carries_the_number(spawned, known_ground):
    responder.respond("teams", "Vinish", PR_FEEDBACK, priority=attention.P_TODAY)
    assert "#1049" in spawned[0]["title"]


def test_the_title_never_cuts_a_word_in_half():
    """It is the subject line of the answer. "...bookings struc" reads like a bug
    in Asta, at the exact moment he is deciding whether to trust the finding."""
    title = responder.title_for("incident", "Vinish", TEMPORAL)
    assert "struc " not in title and not title.endswith("struc")
    long = responder.title_for("debug", "Vinish", "please check " + "verylongword " * 12)
    assert long.endswith("…")
    assert "verylongwor…" not in long          # cut on a space, not mid-word


# --- against the shapes his real feed actually contains -----------------------
# Written after dry-running the classifier over his stored activity rows, where
# the word-form matched "comments on PR 1409" and missed both rows that mattered.

@pytest.mark.parametrize("row", [
    "hi vinish arunkumar https github com maersk global telikos booking service pull 1409 files",
    "hi everyone please review https github com maersk global x pull 1502",
    "check my comments on pullrequest 1409",
])
def test_a_shared_pr_link_is_a_review_ask(row):
    """"same for pr 1049 when he shared the message it should known automatically."
    Teams strips punctuation out of links in the feed, so `pull/1409` arrives as
    `pull 1409` — both forms must match."""
    assert responder.what_it_asks(row) == "pr_review", row


def test_the_pr_number_survives_the_url_form():
    assert responder.pr_number(
        "https github com maersk global telikos booking service pull 1409 files") == "1409"


@pytest.mark.parametrize("row,name", [
    ("vinish kumar mentioned you arunkumar could you please look into the issues",
     "vinish kumar"),
    ("Vinish Kumar mentioned you — Arunkumar — 10:33 AM — In chat with you",
     "Vinish Kumar"),
    ("palikala divya maheswari reacted to your message sure good night",
     "palikala divya maheswari"),
    ("ayashkant baral invited you ooo ayash 28th august", "ayashkant baral"),
])
def test_the_asker_is_pulled_out_of_the_feed_row(row, name):
    """The " — " split in _push_activity does not fire on most renderings, so `who`
    arrives as the entire row. Untouched, the title reads "vinish kumar mentioned
    you arunkumar could you please… asked: …"."""
    assert responder.asker_from(row) == name


def test_a_plain_sentence_is_left_alone():
    assert responder.asker_from("can you check prod", "Vinish") == "Vinish"


def test_a_greeting_with_a_link_is_still_not_an_ask():
    """The other direction. Matching every message containing a URL would spend
    four investigations an hour on people sharing dashboards."""
    assert responder.what_it_asks("komal jayswal mentioned you hi arunkumar call") == ""
    assert responder.what_it_asks("sharing the release notes https confluence x y") == ""


def test_the_feed_chrome_is_not_quoted_back_as_the_message():
    """Left in, the title reads "vinish kumar asked: vinish kumar mentioned you
    arunkumar could you…" and the worker is handed Teams' own UI text as if it
    were what the person said."""
    row = "vinish kumar mentioned you arunkumar could you please look into the failing activity"
    assert responder.message_of(row).startswith("arunkumar could you please")
    title = responder.title_for("debug", "vinish kumar", row)
    assert title.count("vinish kumar") == 1, title
    assert "mentioned you" not in title


def test_a_message_with_no_feed_prefix_is_untouched():
    assert responder.message_of("can you check prod bookings") == "can you check prod bookings"


# --- any ask, not only the three named shapes ---------------------------------
# "not just incident, PR feedback , debug any kind of stuff". Recognising three
# shapes and shrugging at the rest meant a colleague asking something slightly
# differently worded got the old behaviour: a notification and nothing more.

@pytest.mark.parametrize("text", [
    "please share the deployment steps for the booking service",
    "could you send me the swagger for the new endpoint",
    "send me the swagger link when you get a minute",
])
def test_an_ask_with_no_named_shape_is_still_investigated(text):
    assert responder.what_it_asks(text) == "ask", text


@pytest.mark.parametrize("text", [
    "good morning all",
    "thanks, that worked",
    "I am on leave tomorrow",
])
def test_a_statement_is_still_not_an_ask(text):
    """The catch-all must not become "everything". Four investigations an hour
    spent on pleasantries is the noise he already complained about."""
    assert responder.what_it_asks(text) == "", text


def test_the_generic_brief_does_not_pretend_to_know_the_shape():
    """An ask nobody classified is exactly where a confident wrong answer comes
    from, so the brief says to name the readings instead of picking one."""
    brief = responder.brief_for("ask", "Vinish", "can you send the config")
    assert "ambiguous" in brief
    assert "prepare_to_send" in brief


# --- familiar work is continued; new work is offered first --------------------

def test_something_he_has_worked_on_is_familiar(monkeypatch):
    from app import store as st
    monkeypatch.setattr(st, "list_tasks",
                        lambda n=200: [{"title": "Fix BEPTELIKOS-10159 guard",
                                        "prompt": "", "workspace": "booking"}])
    known, why = responder.familiar("any update on BEPTELIKOS-10159?")
    assert known
    assert "BEPTELIKOS-10159" in why


def test_a_workspace_he_works_in_counts(monkeypatch):
    from app import store as st
    monkeypatch.setattr(st, "list_tasks",
                        lambda n=200: [{"title": "x", "prompt": "", "workspace": "booking"}])
    known, why = responder.familiar("the booking service is throwing 500s")
    assert known and "booking" in why


def test_untouched_ground_is_new(monkeypatch):
    from app import store as st
    monkeypatch.setattr(st, "list_tasks", lambda n=200: [])
    assert responder.familiar("can you check FOO-999")[0] is False


def test_new_ground_is_offered_rather_than_started(monkeypatch, spawned):
    """"if it is new related ask me do you want me to work on , can i analyse once
    approved". Starting unasked is the substitution failure in another costume."""
    from app import offers, store as st
    monkeypatch.setattr(st, "list_tasks", lambda n=200: [])
    asked = {}
    monkeypatch.setattr(offers, "propose",
                        lambda **kw: asked.update(kw) or object())
    assert responder.respond("teams", "Vinish", TEMPORAL, priority=attention.P_TODAY) is None
    assert not spawned, "spawned work on new ground without asking"
    assert "look into it" in asked["question"]


def test_the_offer_carries_the_brief_his_yes_will_run(monkeypatch, spawned):
    """His "yes" must run the SAME investigation this would have started —
    otherwise approving is approving something he was never shown."""
    from app import offers, store as st
    monkeypatch.setattr(st, "list_tasks", lambda n=200: [])
    asked = {}
    monkeypatch.setattr(offers, "propose", lambda **kw: asked.update(kw) or object())
    responder.respond("teams", "Vinish", PR_FEEDBACK, priority=attention.P_TODAY)
    assert "Do not assume the reviewer is right" in asked["action"]


def test_familiar_ground_is_acted_on_without_asking(monkeypatch, spawned):
    """The other half — "if it related to already he worked he can directly act on
    and notify me". Asking about work already in flight is the friction he is
    trying to remove."""
    from app import offers, store as st
    monkeypatch.setattr(st, "list_tasks",
                        lambda n=200: [{"title": "PR 1049 review", "prompt": "",
                                        "workspace": ""}])
    monkeypatch.setattr(offers, "propose",
                        lambda **kw: pytest.fail("asked about work already underway"))
    assert responder.respond("teams", "Vinish", PR_FEEDBACK, priority=attention.P_TODAY)
    assert spawned


@pytest.mark.parametrize("text,is_ask", [
    ("send me the swagger link when you get a minute", True),
    ("let me know once it is deployed", True),
    # …but aimed at somebody else, it is not his to answer.
    ("send the release notes to the team", False),
    ("I will send the notes tomorrow", False),
])
def test_a_bare_imperative_aimed_at_him_is_an_ask(text, is_ask):
    """No "please", no "can you", and unmistakably a request. Anchored on the
    pronoun, because "send the notes to the team" is somebody else's job."""
    from app import triage
    assert triage.classify("Vinish", text).action is is_ask, text


# --- a live ask, not the day's backlog ----------------------------------------
#
#   "at the time if someone pings for example now asking , asking to debug , going
#    to debug now and sharing report ... already for the dead old message and done
#    work if this shares then it is worst , if it is his old work someone giving
#    feedback then it work now makes sense"
#
# `chat_watch` opens a thread for the first time and finds the whole day in it —
# new to Asta, old to the world. Task #74 went off to check production issues Arun
# had already analysed and answered hours earlier.
#
# All three of his cases fall out of one test on the MESSAGE's own timestamp.

def test_something_asked_just_now_is_investigated():
    import time
    assert not responder.should_respond("incident", attention.P_TODAY, "live",
                                        sent_at=time.time())


def test_this_morning_is_not_a_live_ask():
    import time
    why = responder.should_respond("incident", attention.P_TODAY, "old",
                                   sent_at=time.time() - 6 * 3600)
    assert "not a live ask" in why


def test_feedback_on_old_work_is_still_fresh_input():
    """His distinction. A review of something he built months ago arrived just
    now, and acting on it is exactly right — the age of the WORK is irrelevant."""
    import time
    assert not responder.should_respond("pr_review", attention.P_TODAY, "fb",
                                        sent_at=time.time() - 60)


def test_a_missing_timestamp_is_treated_as_fresh():
    """Teams does not always render a machine-readable time. Refusing everything
    without one would silently drop real asks, and an absent timestamp is a
    scraping gap rather than evidence of age."""
    assert not responder.should_respond("incident", attention.P_TODAY, "notime",
                                        sent_at=None)


def test_the_reader_passes_the_timestamp_through():
    """A gate nothing supplies is a gate that never closes."""
    import inspect
    from app import chat_watch
    assert 'sent_at=m.get("sent_at")' in inspect.getsource(chat_watch.sweep)


def test_the_age_limit_is_minutes_not_days():
    assert responder.MAX_AGE_MINUTES <= 240
