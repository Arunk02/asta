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

def test_a_production_question_is_investigated(spawned):
    """His example. Vinish asks whether prod bookings are stuck; Asta goes and
    looks instead of forwarding the question to a man who is not at his desk."""
    t = responder.respond("teams", "Vinish", TEMPORAL, priority=attention.P_TODAY)
    assert t, "nothing was investigated"
    assert spawned[0]["kind"] == "analysis"
    assert "Temporal" in spawned[0]["prompt"]


def test_pr_feedback_is_verified_rather_than_believed(spawned):
    """"check right whether it is true or wrong". The reviewer is not assumed
    correct — that assumption is what pushed seven unreviewed edits on 27 August."""
    responder.respond("teams", "Vinish", PR_FEEDBACK, priority=attention.P_TODAY)
    prompt = spawned[0]["prompt"]
    assert "1049" in prompt
    assert "Do not assume the reviewer is right" in prompt
    assert "do not change any code" in prompt.lower()


def test_a_debug_request_is_picked_up(spawned):
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

def test_it_can_only_ever_read(spawned):
    """The whole design rests on this. A code task edits a repository and pushes;
    an analysis task reads and reports. Nothing here may spawn the former."""
    for text in (TEMPORAL, PR_FEEDBACK, DEBUG):
        store.kv_set(responder._DONE_KEY, "")
        store.kv_set(responder._RATE_KEY, "")
        responder.respond("teams", "Vinish", text, priority=attention.P_NOW)
    assert spawned
    assert {c["kind"] for c in spawned} == {"analysis"}


def test_it_never_sends_anything_itself(spawned):
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


def test_the_brief_is_self_contained(spawned):
    """The worker has no chat context — `delegate_task` says so. A prompt that
    says "the message above" reaches a worker that cannot see any message."""
    responder.respond("teams", "Vinish", TEMPORAL, priority=attention.P_TODAY)
    prompt = spawned[0]["prompt"]
    assert TEMPORAL in prompt, "the message itself is not in the brief"
    assert "the above" not in prompt.lower()
    assert "Vinish" in prompt


def test_ten_people_pinging_is_not_ten_investigations(spawned, monkeypatch):
    """His burst scenario, and the reason a rate limit is not optional: each of
    these is a full agentic turn against production systems."""
    monkeypatch.setattr(responder, "MAX_PER_HOUR", 3)
    for i in range(10):
        responder.respond("teams", f"Person{i}", f"{TEMPORAL} number {i}",
                          priority=attention.P_TODAY)
    assert len(spawned) == 3


def test_the_same_ask_is_investigated_once(spawned):
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


def test_the_answer_arrives_still_attached_to_its_question(spawned):
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


def test_a_pr_title_carries_the_number(spawned):
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
