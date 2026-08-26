"""Offers — "can I analyse this?" → yes → it goes.

Arun's ask: "if any issues it will notify, ask me can i analyse yes it will go
analyse, ci watch and raise PR and once i tell where to share it will share the
build for the approval."

The properties that matter: nothing runs unasked, a yes from ANY channel starts
it, a yes cannot run the same work twice, and an offer he ignored must not fire
days later.
"""

from __future__ import annotations

import time

import pytest

from app import offers, store, main


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    offers.clear()
    monkeypatch.delenv("ASTA_OFFER_TTL", raising=False)
    yield


# --- the basic contract -----------------------------------------------------

def test_an_offer_survives_and_is_returned():
    offers.offer("analyse", "CI failed", "build.yml on main", "Analyse it?")
    o = offers.pending()
    assert o and o.kind == "analyse" and "build.yml" in o.context


def test_accept_consumes_it_so_a_double_yes_cannot_run_it_twice():
    offers.offer("analyse", "CI failed", "ctx", "Analyse it?")
    assert offers.accept() is not None
    assert offers.accept() is None                 # the second yes finds nothing
    assert offers.pending() is None


def test_decline_closes_it():
    offers.offer("analyse", "CI failed", "ctx", "Analyse it?")
    assert offers.decline() is not None
    assert offers.pending() is None


def test_an_offer_is_persisted_so_a_restart_does_not_lose_the_question():
    """It was asked on his phone; he may answer after a restart. A question that
    silently evaporates teaches him not to trust the assistant."""
    offers.offer("analyse", "CI failed", "ctx", "Analyse it?")
    raw = store.kv_get(offers.KEY)
    assert raw and "analyse" in raw               # it is in the DB, not in RAM


def test_a_stale_offer_expires_rather_than_firing_later(monkeypatch):
    """A 'yes' tomorrow must not kick off work he has forgotten proposing."""
    monkeypatch.setenv("ASTA_OFFER_TTL", "3600")
    o = offers.offer("analyse", "CI failed", "ctx", "Analyse it?")
    assert offers.pending() is not None
    monkeypatch.setattr(offers.time, "time", lambda: o.created + 3601)
    assert offers.pending() is None


def test_ttl_zero_means_never_expires(monkeypatch):
    monkeypatch.setenv("ASTA_OFFER_TTL", "0")
    o = offers.offer("analyse", "CI failed", "ctx", "Analyse it?")
    monkeypatch.setattr(offers.time, "time", lambda: o.created + 10**7)
    assert offers.pending() is not None


def test_only_one_offer_is_open_at_a_time():
    """Two open questions plus a bare 'yes' is ambiguous — guessing would be the
    confident wrong move."""
    offers.offer("analyse", "first", "c", "a?")
    offers.offer("raise_pr", "second", "c", "b?")
    o = offers.pending()
    assert o.kind == "raise_pr" and o.subject == "second"


def test_corrupt_state_never_crashes():
    store.kv_set(offers.KEY, "{not json")
    assert offers.pending() is None


# --- what he actually reads -------------------------------------------------

def test_render_gives_context_first_then_one_question():
    o = offers.offer("analyse", "🔴 CI failed: asta",
                     "build.yml on main\nhttps://gh/run/1", "Want me to analyse the failure?")
    text = o.render()
    assert text.index("build.yml") < text.index("Want me to analyse")
    assert 'reply “yes”' in text                   # he is told exactly how to accept


# --- the chain --------------------------------------------------------------

def test_ci_failure_offers_analysis_with_the_precise_detail():
    o = offers.for_ci_failure("Arunk02/asta", "build.yml", "main",
                              "https://gh/run/1", "flaky login test")
    assert o.kind == "analyse"
    assert "asta" in o.subject
    assert "build.yml on main" in o.context and "flaky login test" in o.context
    assert o.payload["repo"] == "Arunk02/asta"


def test_analysis_then_offers_the_pr_carrying_context_forward():
    first = offers.for_ci_failure("Arunk02/asta", "build.yml", "main", "https://gh/run/1")
    second = offers.after_analysis(first, "The token refresh races on startup.")
    assert second.kind == "raise_pr"
    assert "races on startup" in second.context
    assert second.payload["repo"] == "Arunk02/asta"      # nothing is lost between steps


def test_the_pr_step_asks_where_rather_than_assuming():
    first = offers.for_ci_failure("Arunk02/asta", "build.yml", "main", "https://gh/run/1")
    third = offers.after_pr(first, "https://github.com/Arunk02/asta/pull/11")
    assert third.kind == "share_build"
    assert "Where should I share" in third.prompt
    assert third.payload["pr_url"].endswith("/11")


# --- the reply routing ------------------------------------------------------

def test_yes_maps_to_a_prompt_that_investigates_without_changing_code():
    o = offers.for_ci_failure("Arunk02/asta", "build.yml", "main", "https://gh/run/1")
    p = main._offer_prompt(o)
    assert "Do NOT change production code yet" in p
    assert "build.yml on main" in p                      # the context travels with it
    assert "whether to fix it and raise the PR" in p     # and it offers the next step
    assert "root cause" in p.lower()                     # senior analysis, not a log skim


def test_the_pr_prompt_uses_his_personal_account_and_chains_on():
    o = offers.offer("raise_pr", "Analysed", "cause: race on startup", "Raise the PR?")
    p = main._offer_prompt(o)
    assert "personal account" in p
    assert "where he wants the build shared" in p


def test_the_share_prompt_stages_rather_than_sending():
    """The one hard rule survives the chain: nothing leaves unconfirmed."""
    o = offers.offer("share_build", "PR raised", "https://gh/pull/11", "Where to share?")
    p = main._offer_prompt(o, where="Priya")
    assert "Priya" in p
    assert "prepare_to_send" in p and "do NOT send it yourself" in p


def test_affirm_and_decline_patterns_are_distinct():
    assert main._AFFIRM.match("yes")
    assert main._DECLINE.match("not now")
    assert main._DECLINE.match("no")
    assert not main._DECLINE.match("yes")
    # A destination is neither — it must reach the share step as the answer.
    assert not main._AFFIRM.match("share it with Priya")
    assert not main._DECLINE.match("share it with Priya")


# --- the glue: a "yes" from any channel really does start the work ----------

def test_a_bare_yes_from_the_phone_starts_the_analysis(monkeypatch):
    """End to end through the real router: the offer went to WhatsApp, and his
    one-word reply comes back on WhatsApp hours later."""
    import asyncio
    started = {}

    def fake_start(conv, prompt, sink, channel):
        started["prompt"], started["channel"] = prompt, channel
        return "task"

    monkeypatch.setattr(main, "_start_turn", fake_start)
    offers.for_ci_failure("Arunk02/asta", "build.yml", "main", "https://gh/run/1")

    out = asyncio.run(main._dispatch({"id": "c1", "model": "claude"}, "yes",
                                     _Sink(), "whatsapp"))
    assert out == "task"                               # a turn really started
    assert "root cause" in started["prompt"].lower()
    assert started["channel"] == "whatsapp"
    assert offers.pending() is None                    # and the offer is consumed


def test_a_no_closes_the_offer_without_starting_work(monkeypatch):
    import asyncio
    monkeypatch.setattr(main, "_start_turn",
                        lambda *a: pytest.fail("declining must not start a turn"))
    offers.for_ci_failure("Arunk02/asta", "build.yml", "main", "https://gh/run/1")
    sink = _Sink()
    assert asyncio.run(main._dispatch({"id": "c1", "model": "claude"}, "not now",
                                      sink, "whatsapp")) is None
    assert offers.pending() is None
    assert any("Dropped it" in str(p) for p in sink.sent)


def test_naming_a_destination_answers_the_share_step(monkeypatch):
    import asyncio
    started = {}
    monkeypatch.setattr(main, "_start_turn",
                        lambda c, p, s, ch: started.update(prompt=p) or "task")
    offers.offer("share_build", "PR raised", "https://gh/pull/11", "Where to share?")
    asyncio.run(main._dispatch({"id": "c1", "model": "claude"}, "share it with Priya",
                               _Sink(), "whatsapp"))
    assert "Priya" in started["prompt"]                 # his words became the destination


def test_changing_the_subject_drops_the_offer_instead_of_holding_it(monkeypatch):
    """Otherwise a 'yes' to something unrelated an hour later would fire it."""
    import asyncio
    monkeypatch.setattr(main, "_start_turn", lambda *a: "task")
    offers.for_ci_failure("Arunk02/asta", "build.yml", "main", "https://gh/run/1")
    asyncio.run(main._dispatch({"id": "c1", "model": "claude"},
                               "what meetings do I have tomorrow?", _Sink(), "whatsapp"))
    assert offers.pending() is None


# --- the general form: any flow, not just the CI chain ----------------------

def test_an_offer_can_carry_its_own_next_step():
    """The reason this stopped being a three-kind enum. Implementing a ticket,
    chasing a review and updating a status all want the same shape, and none of
    them fit a fixed list."""
    o = offers.propose("PROJ-412 read", "Acceptance criteria are in the comments.",
                       "Implement it on a branch?",
                       "Implement PROJ-412 on a branch and run the tests.")
    p = main._offer_prompt(offers.pending())
    assert "Implement PROJ-412 on a branch" in p
    assert "Acceptance criteria are in the comments" in p     # context travels
    assert o.kind == "next"


def test_a_carried_action_beats_the_built_in_kinds():
    """A recipe kind with its own action must not silently fall through to the
    hardcoded CI wording — that would run analysis instead of what was proposed."""
    o = offers.offer("analyse", "s", "c", "q?", action="Follow up with Priya on the schema.")
    p = main._offer_prompt(o)
    assert "Follow up with Priya" in p
    assert "pull the failing job" not in p


def test_a_proposed_step_ends_by_offering_the_next_one():
    """Otherwise the chain stops after one hop and he has to restart it by hand."""
    o = offers.propose("s", "c", "q?", "Update the ticket status.")
    assert "propose_next" in main._offer_prompt(o)


def test_propose_next_refuses_an_empty_step():
    from app import agent
    assert "concrete" in agent.propose_next("  ").lower()
    assert offers.pending() is None


def test_his_words_reach_a_proposed_step_when_he_answers_with_them():
    o = offers.propose("s", "c", "Where should it go?", "Share the summary.")
    assert "with Priya" in main._offer_prompt(o, where="with Priya")


# --- staged writes: what he approved is what runs ---------------------------

def test_a_staged_write_records_the_exact_call_not_an_instruction():
    o = offers.staged_write("jira_comment", {"key": "PROJ-1", "text": "Blocked on schema."},
                            "💬 Comment on PROJ-1", "Blocked on schema.", "Post it?")
    assert o.mechanical()
    assert o.op == {"name": "jira_comment",
                    "args": {"key": "PROJ-1", "text": "Blocked on schema."}}


def test_a_prompt_offer_is_not_mechanical():
    assert not offers.propose("s", "c", "q?", "do the thing").mechanical()
    assert not offers.for_ci_failure("r/x", "w", "b", "u").mechanical()


def test_a_yes_to_a_staged_write_runs_the_call_and_never_a_brain(monkeypatch):
    import asyncio
    ran = {}

    async def fake_run(op_spec):
        ran["op"] = op_spec
        return "💬 Commented on PROJ-1."

    monkeypatch.setattr(main.ops, "run", fake_run)
    monkeypatch.setattr(main, "_start_turn",
                        lambda *a: pytest.fail("an approved write must not spawn a brain turn"))
    offers.staged_write("jira_comment", {"key": "PROJ-1", "text": "Blocked."},
                        "s", "Blocked.", "Post it?")
    sink = _Sink()
    out = asyncio.run(main._dispatch({"id": "c1", "model": "claude"}, "yes", sink, "whatsapp"))
    assert out is None                                   # no turn was started
    assert ran["op"]["args"]["text"] == "Blocked."       # the recorded words, verbatim
    assert any("Commented on PROJ-1" in str(p) for p in sink.sent)


def test_a_failed_write_is_reported_rather_than_swallowed(monkeypatch):
    """He said yes to a comment landing on a ticket. If it didn't, silence means
    he finds out from the colleague who never got it."""
    import asyncio

    async def boom(op_spec):
        raise RuntimeError("Jira: 403")

    monkeypatch.setattr(main.ops, "run", boom)
    offers.staged_write("jira_comment", {"key": "P-1", "text": "x"}, "s", "c", "Post it?")
    sink = _Sink()
    asyncio.run(main._dispatch({"id": "c1", "model": "claude"}, "yes", sink, "whatsapp"))
    said = " ".join(str(p) for p in sink.sent)
    assert "403" in said and "Couldn't" in said


def test_declining_a_staged_write_runs_nothing(monkeypatch):
    import asyncio
    monkeypatch.setattr(main.ops, "run",
                        lambda op: pytest.fail("a declined write must never run"))
    offers.staged_write("jira_transition", {"key": "P-1", "to_status": "Done"},
                        "s", "c", "Move it?")
    asyncio.run(main._dispatch({"id": "c1", "model": "claude"}, "no", _Sink(), "whatsapp"))
    assert offers.pending() is None


# --- surviving an upgrade ---------------------------------------------------

def test_an_offer_written_by_an_older_version_still_loads():
    """A row missing the newer fields must not evaporate — the question was asked
    on his phone and he is about to answer it."""
    import json
    store.kv_set(offers.KEY, json.dumps({
        "id": "abc", "kind": "analyse", "subject": "🔴 CI failed",
        "context": "build.yml", "prompt": "Analyse it?", "created": time.time(),
        "payload": {"repo": "r/x"}}))
    o = offers.pending()
    assert o and o.kind == "analyse" and o.action == "" and not o.mechanical()


def test_an_offer_carrying_unknown_fields_still_loads():
    """Forward compatibility in the other direction: downgrading loses the extra,
    not the question."""
    import json
    store.kv_set(offers.KEY, json.dumps({
        "id": "abc", "kind": "next", "subject": "s", "context": "c", "prompt": "q?",
        "created": time.time(), "invented_later": {"nested": 1}}))
    assert offers.pending() is not None


def test_a_json_list_is_not_mistaken_for_an_offer():
    import json
    store.kv_set(offers.KEY, json.dumps(["not", "an", "offer"]))
    assert offers.pending() is None


# --- what he reads ----------------------------------------------------------

def test_render_tells_him_how_to_say_no_as_well_as_yes():
    """Declining should be the cheaper of the two answers; spelling out only 'yes'
    made a no feel like it owed an explanation."""
    text = offers.propose("s", "c", "Do it?", "do it").render()
    assert "yes" in text and "no" in text


class _Sink:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, payload):
        self.sent.append(payload)
