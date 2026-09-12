"""Keeping a promise after the turn that made it has ended.

"track on with vinish and get this all these 3 PR's ... merged by tmr EOD, if
not done let me know what issue notify me" is not a question and not a code
task. Asta could do none of it: a chat turn answers once and forgets,
`pr_watch_loop` follows only PRs Asta itself shipped (two of those three were
raised by hand, so they were invisible), and a reminder fires once and tells
nobody but Arun — which moves the work back onto him instead of chasing it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import followup, store

_A = "https://github.com/o/r/pull/1"
_B = "https://github.com/o/r/pull/2"


def _pr(state="OPEN", decision="", checks="green", merged=None, title="PR"):
    roll = {"green": [{"conclusion": "SUCCESS"}], "red": [{"conclusion": "FAILURE"}],
            "pending": [{"status": "IN_PROGRESS"}]}[checks]
    return {"state": state, "mergedAt": merged, "reviewDecision": decision,
            "statusCheckRollup": roll, "url": _A, "title": title,
            "reviews": [], "comments": []}


@pytest.fixture
def prs(monkeypatch):
    """What GitHub says, per url."""
    table: dict = {}

    async def fake(url):
        return table.get(url, {})
    from app import tasks
    monkeypatch.setattr(tasks, "_pr_state", fake)
    return table


# --- the tracker itself ------------------------------------------------------

def test_tracking_the_same_prs_twice_does_not_double_up():
    """Otherwise saying it again quietly produces two trackers, both nudging the
    same person about the same PRs."""
    a = followup.track("get them merged", [_A, _B], "Vinish Kumar")
    b = followup.track("get them merged", [_B, _A], "Vinish Kumar")
    assert a["id"] == b["id"]
    assert len(followup.list_open()) == 1


def test_tracking_nothing_is_refused():
    with pytest.raises(ValueError):
        followup.track("chase it", [])


def test_stopping_it_takes_it_off_the_list():
    row = followup.track("g", [_A])
    assert "Stopped" in followup.stop(row["id"])
    assert followup.list_open() == []


# --- what it says ------------------------------------------------------------

def test_it_names_the_blocker_not_just_that_it_is_open(prs):
    """He asked to be told *what issue*. "Not merged" is a restatement, not a
    blocker — the answer has to say what to do about it."""
    cases = [(_pr(checks="red"), "CI red"),
             (_pr(decision="CHANGES_REQUESTED"), "changes requested"),
             (_pr(checks="pending"), "CI still running"),
             (_pr(decision="APPROVED"), "approved, waiting to be merged"),
             (_pr(), "no review yet"),
             (_pr(state="CLOSED"), "closed without merging")]
    for pr, want in cases:
        assert followup._blocker(pr) == want, want


def test_a_merged_pr_has_no_blocker():
    assert followup._blocker(_pr(state="MERGED", merged="2026-09-08T10:00:00Z")) == ""


def test_the_deadline_is_warned_before_it_lands_not_after(prs):
    """"Let me know if it isn't done" said after EOD is a report; said four hours
    before, it is still something he can act on."""
    prs[_A] = _pr(checks="red")
    now = time.time()
    row = followup.track("3 PRs merged", [_A], "Vinish Kumar", due_at=now + 3 * 3600)

    notes, done = asyncio.run(followup.check_one(row, now))
    assert not done
    assert notes and "⏰" in notes[0] and "CI red" in notes[0]


def test_a_deadline_still_far_off_says_nothing(prs):
    prs[_A] = _pr(checks="red")
    now = time.time()
    row = followup.track("g", [_A], due_at=now + 48 * 3600)
    notes, done = asyncio.run(followup.check_one(row, now))
    assert notes == [] and not done


def test_the_warning_fires_once_not_every_poll(prs):
    """A watcher that repeats itself every fifteen minutes gets muted."""
    prs[_A] = _pr(checks="red")
    now = time.time()
    row = followup.track("g", [_A], due_at=now + 3600)
    first, _ = asyncio.run(followup.check_one(row, now))
    second, _ = asyncio.run(followup.check_one(row, now + 900))
    assert first and second == []


def test_all_merged_closes_it(prs):
    prs[_A] = _pr(state="MERGED", merged="2026-09-08T10:00:00Z")
    prs[_B] = _pr(state="MERGED", merged="2026-09-08T10:00:00Z")
    row = followup.track("3 PRs merged", [_A, _B])
    notes, done = asyncio.run(followup.check_one(row))
    assert done and row["status"] == "done"
    assert "✅" in notes[0]


def test_an_unreadable_pr_is_never_counted_as_merged(prs):
    """gh failing is not good news, and treating it as done closes the tracker
    on a PR nobody has looked at."""
    row = followup.track("g", [_A])            # prs table empty → {} back
    _notes, done = asyncio.run(followup.check_one(row))
    assert not done and row["status"] == "open"


def test_still_red_is_not_reported_as_movement(prs):
    prs[_A] = _pr(checks="red")
    row = followup.track("g", [_A])
    asyncio.run(followup.check_one(row))
    moved_at = row["last_moved_at"]
    asyncio.run(followup.check_one(row, time.time() + 900))
    assert row["last_moved_at"] == moved_at


def test_a_real_state_change_counts_as_movement(prs):
    prs[_A] = _pr(checks="red")
    row = followup.track("g", [_A])
    asyncio.run(followup.check_one(row))
    prs[_A] = _pr(decision="APPROVED")
    later = time.time() + 900
    asyncio.run(followup.check_one(row, later))
    assert row["last_moved_at"] == later


# --- chasing the person ------------------------------------------------------

def test_a_stalled_pr_drafts_a_chase_and_never_sends_it(prs):
    """His standing rule: nothing leaves the machine without his yes. A tracker
    that quietly messages colleagues at 3am is what that rule exists to prevent."""
    prs[_A] = _pr(checks="red")
    now = time.time()
    row = followup.track("3 PRs merged", [_A], "Vinish Kumar", due_at=now + 3600)
    row["last_moved_at"] = now - followup.STALL_SECONDS - 60

    note = asyncio.run(followup._nudge(row, 1, now))
    assert "approve task" in note and _A in note

    drafts = [t for t in store.list_tasks(limit=10) if t["kind"] == "teams_draft"]
    assert drafts and drafts[0]["teams_chat"] == "Vinish Kumar"
    assert drafts[0]["status"] == "awaiting_approval"


def test_nothing_is_drafted_while_the_work_is_moving(prs):
    now = time.time()
    row = followup.track("g", [_A], "Vinish Kumar")
    row["last_moved_at"] = now - 60          # moved a minute ago
    assert asyncio.run(followup._nudge(row, 1, now)) == ""


def test_nobody_is_nudged_twice_in_the_same_window():
    now = time.time()
    row = followup.track("g", [_A], "Vinish Kumar")
    row["last_moved_at"] = now - followup.STALL_SECONDS - 60
    assert asyncio.run(followup._nudge(row, 1, now))
    assert asyncio.run(followup._nudge(row, 1, now + 3600)) == ""


def test_no_person_means_no_draft():
    now = time.time()
    row = followup.track("g", [_A])
    row["last_moved_at"] = now - followup.STALL_SECONDS - 60
    assert asyncio.run(followup._nudge(row, 1, now)) == ""


# --- wiring ------------------------------------------------------------------

def test_the_loop_is_supervised_like_every_other_promise():
    """Unsupervised, a tracker that dies stops being true silently — which is
    worse than never having promised."""
    import inspect
    from app import main
    assert 'daemon.start("followup"' in inspect.getsource(main)


def test_the_brain_can_actually_reach_it():
    from app import capabilities
    for name in ("track_until_done", "list_tracked", "stop_tracking"):
        cap = capabilities.get(name)
        assert cap is not None and cap.fn is not None, name


def test_an_unreadable_deadline_is_refused_not_guessed():
    """Guessing would track silently and never warn — worse than no deadline."""
    from app import agent
    out = agent.track_until_done("g", [_A], "Vinish Kumar", when="tomorrow EOD")
    assert "can't read" in out and "ISO" in out


def test_a_real_deadline_is_accepted():
    from app import agent
    out = agent.track_until_done("3 PRs merged", [_A, _B], "Vinish Kumar",
                                 when="2026-09-08T18:00")
    assert "Tracking #" in out and "2 PR(s)" in out and "Vinish Kumar" in out


# --- retrieval, which the new tools perturbed --------------------------------

def test_a_message_naming_a_jira_key_always_gets_the_jira_tools():
    """Adding three capabilities shifted the corpus statistics enough that
    "comment on ABC-123 that it's done" ranked `morning_brief` 0.588 against
    `jira_comment` 0.581 — and only the top hit's group comes along, so a message
    naming an issue outright lost every Jira tool to a 0.007 gap. An identifier
    the user typed is not a guess and does not go through the ranker."""
    from app import capabilities, tool_index
    jira = {n for n, c in capabilities.registry().items() if c.group == "jira"}
    for q in ("comment on ABC-123 that it is done",
              "what is BEPTELIKOS-10247 about",
              "move ABC-1 to done"):
        picked = tool_index.select(q)
        assert picked is None or jira <= set(picked), q


def test_ordinary_prose_is_not_mistaken_for_an_issue_key():
    from app import tool_index
    for q in ("the CI-1 job", "merged PR-665 already", "it is UTF-8 encoded",
              "per ISO-8601", "see RFC-7231", "an X86-64 build"):
        forced = tool_index.required_for(q)
        assert not any(n.startswith("jira") for n in forced), q


def test_the_chase_tool_does_not_cost_a_jira_ask_its_tools():
    """Its first note quoted generic phrasings — "tell me if it isn't done" —
    which is the language of half the messages he sends, and it out-ranked
    `jira_comment` on a message naming a Jira issue.

    Asserted on the SELECTION, not on which capability ranks first. Rank order
    across the middle of the pack moves whenever the corpus changes and is not a
    guarantee anything depends on; "a message naming an issue gets the Jira
    tools" is, and `required_for` makes it true regardless of the ranking."""
    from app import capabilities, tool_index
    jira = {n for n, c in capabilities.registry().items() if c.group == "jira"}
    picked = tool_index.select("comment on ABC-123 that it is done")
    assert picked is None or jira <= set(picked)


# --- not chasing the chaser --------------------------------------------------

def test_no_second_draft_while_the_first_is_still_unsent(prs):
    """The time window alone produced four identical "any chance you can take a
    look at these today?" drafts for Vinish across three days — none sent, none
    rejected, each asking Arun the same question he had already not answered.

    A draft he has not acted on is not a reason to write him another one."""
    now = time.time()
    row = followup.track("3 PRs merged", [_A], "Vinish Kumar", due_at=now + 3600)
    row["last_moved_at"] = now - followup.STALL_SECONDS - 60

    first = asyncio.run(followup._nudge(row, 1, now))
    assert "approve task" in first, "the first one is the point of the feature"

    row["last_nudged_at"] = 0                       # window is not what blocks it
    row["last_moved_at"] = now - followup.STALL_SECONDS - 60
    assert asyncio.run(followup._nudge(row, 1, now + 99999)) == ""


def test_once_he_acts_on_it_chasing_can_resume():
    """Blocked by an OUTSTANDING draft, not by ever having drafted one."""
    from app import store
    t = store.create_task("Nudge Vinish Kumar — x", "teams_draft", "hi", None,
                          teams_chat="Vinish Kumar")
    store.update_task(t["id"], status="awaiting_approval")
    assert followup._draft_pending("Vinish Kumar") is True

    store.update_task(t["id"], status="sent")
    assert followup._draft_pending("Vinish Kumar") is False


def test_a_draft_for_somebody_else_does_not_block_it():
    from app import store
    t = store.create_task("Nudge Ravi — x", "teams_draft", "hi", None,
                          teams_chat="Ravi Menon")
    store.update_task(t["id"], status="awaiting_approval")
    assert followup._draft_pending("Vinish Kumar") is False
