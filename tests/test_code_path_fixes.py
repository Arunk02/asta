"""The coding path, 2026-09-07: one change that became two tasks and two commits.

Arun asked for `transportAssetPriority`, a field crossing two repos. What he got:
task #88 and task #89, four identical "done" messages, two divergent commits of
the same change on two branches neither of which Asta was tracking, and a
completion that asked him for a branch it already had.

Every number below came out of his own repos and database.
"""

from __future__ import annotations

import json

import pytest

from app import attention, chat_watch, delivery, refresh, store, tasks


# --- the context map the agent is told to trust ------------------------------------

def test_the_gap_is_counted_in_commits_not_days(monkeypatch, tmp_path):
    """Days measure how long nobody ran a script. Commits measure how much the
    agent does not know: booking-service was stamped against 6ee7d83b while
    develop had moved 143 commits."""
    monkeypatch.setattr(refresh.ws_mod, "get", lambda w: None)
    assert refresh.context_gap("nope") == []


def test_a_current_workspace_adds_nothing_to_the_prompt(monkeypatch):
    monkeypatch.setattr(refresh, "context_gap",
                        lambda w: [{"repo": "a", "verified": "abc", "behind": 0}])
    assert refresh.trust_note("booking") == ""


def test_a_stale_map_is_named_with_its_number(monkeypatch):
    monkeypatch.setattr(refresh, "context_gap",
                        lambda w: [{"repo": "telikos-booking-service",
                                    "verified": "6ee7d83b", "behind": 143}])
    note = refresh.trust_note("booking")
    assert "143 commits behind" in note
    assert "telikos-booking-service" in note


def test_never_verified_counts_as_stale(monkeypatch):
    """No context at all is not a gentler version of slightly-old context."""
    monkeypatch.setattr(refresh, "context_gap",
                        lambda w: [{"repo": "r", "verified": "", "behind": -1}])
    assert "never verified" in refresh.trust_note("booking")


def test_the_instruction_is_the_point_not_the_number(monkeypatch):
    """Everywhere else the agent is told to route through the map and never scan
    blind. Without an explicit exception it reads a silent map as a definitive
    no — which is how "zero references anywhere in this repo" gets said about a
    repo nobody looked at."""
    monkeypatch.setattr(refresh, "context_gap",
                        lambda w: [{"repo": "r", "verified": "a", "behind": 99}])
    note = refresh.trust_note("booking")
    assert "not 'not in the repo'" in note.replace("’", "'")


# --- one completion, one message ----------------------------------------------------

def test_a_finished_task_is_not_finished_twice():
    """#88 pushed four identical "✅ DONE" messages — 12:42, 12:53, 12:59, 13:07.
    `_finish_code` re-enters itself on escalation and on a repo hop, and its done
    branch had no memory."""
    assert "done" in tasks._ALREADY_REPORTED


def test_final_still_means_called_off():
    """FINAL must NOT gain "done": a finished task can still be steered, and the
    checks that use it are asking "was this cancelled?", not "is it over?"."""
    assert "done" not in tasks.FINAL
    assert set(tasks.FINAL) < set(tasks._ALREADY_REPORTED)


# --- the run is told where it already is ------------------------------------------------

def test_the_run_is_told_its_branch(monkeypatch):
    """#88 reported "blocked, no feature branch cut for this repo yet under this
    task" while standing on the branch Asta cut for it, with its own commit on
    it, and then asked Arun for a branch."""
    store.kv_set("task_branch:7", "feature/asta-7-thing")
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: "/tmp/wt")
    monkeypatch.setattr(tasks, "_wt", None, raising=False)
    note = tasks._branch_note(7, {"workspace": "booking"})
    assert "feature/asta-7-thing" in note
    assert "Do NOT create a branch" in note
    assert "one task, not two" in note


def test_no_prepared_branch_means_no_claim():
    assert tasks._branch_note(999999, {"workspace": "booking"}) == ""


@pytest.mark.parametrize("f", ["agents/micro.md", "agents/solo.md"])
def test_both_pipelines_carry_the_branch_rule(f):
    """One rule, both tiers. Neither mentioned branches at all, which is why the
    agent did the only thing left to it and cut its own."""
    from pathlib import Path
    body = Path(f).read_text()
    assert "## Branch" in body
    assert "Never `git checkout -b`" in body


# --- a change across two repos is one task ---------------------------------------------

_TAIL_88 = (
    "- **telikos-activityplanworkflow-service (TMS-facing side):** same field "
    "confirmed present upstream in TransportOrder.v9.avsc, but our vendored copy "
    "+ TmsServiceImpl mapping is still outstanding — blocked, no feature branch "
    "cut for this repo yet under this task."
)


def _two_repos_one_prepared(monkeypatch):
    """A workspace of two repos where the task only ever got a checkout of one.

    Patched on the real module, not via sys.modules: `from . import worktrees`
    resolves the package attribute, so a sys.modules entry is simply ignored —
    which once let a stubbed test run the real Teams path.
    """
    from pathlib import Path

    from app import worktrees
    monkeypatch.setattr(tasks, "code_cwd", lambda ws: "/ws")
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: "/ws/wt")
    monkeypatch.setattr(worktrees, "repos_in", lambda p: (
        [Path("/ws/wt/telikos-booking-service")] if str(p).endswith("wt")
        else [Path("/ws/telikos-booking-service"),
              Path("/ws/telikos-activityplanworkflow-service")]))


def test_the_repo_it_could_not_reach_is_detected(monkeypatch):
    _two_repos_one_prepared(monkeypatch)
    assert tasks._repos_still_needed(88, {"workspace": "booking"}, _TAIL_88) == [
        "telikos-activityplanworkflow-service"]


def test_naming_a_repo_without_saying_it_is_left_over_continues_nothing(monkeypatch):
    """Both halves are required. A run legitimately mentions the repo upstream
    of it; that is background, not a request for another window."""
    _two_repos_one_prepared(monkeypatch)
    assert tasks._repos_still_needed(
        88, {"workspace": "booking"},
        "The field arrives from telikos-activityplanworkflow-service. All done "
        "here, build green.") == []


def test_a_clean_finish_continues_nothing(monkeypatch):
    """Naming the repo you just finished is not asking for another window."""
    monkeypatch.setattr(tasks, "code_cwd", lambda ws: "/ws")
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: "/ws/wt")
    assert tasks._repos_still_needed(
        88, {"workspace": "booking"},
        "Done. Build green in telikos-booking-service.") == []


def test_a_repo_it_already_has_is_never_re_prepared(monkeypatch):
    """If it could work there and did not, that is a decision, not a missing tree."""
    monkeypatch.setattr(tasks, "code_cwd", lambda ws: "/ws")
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: "/ws")
    assert tasks._repos_still_needed(88, {"workspace": "booking"}, _TAIL_88) == []


# --- a later leg does not redo an earlier leg's work -------------------------------------

def test_each_leg_is_shown_what_this_task_already_committed(monkeypatch):
    """#89's escalated leg opened its worktree, recognised nothing, and
    implemented the whole change a second time. The two commits differ, so there
    are now two versions of one change."""
    monkeypatch.setattr(tasks, "committed_so_far", lambda tid, t: [
        {"repo": "telikos-booking-service",
         "branch": "feature/asta-89-x",
         "commits": ["552931a Add transportAssetPriority mapping"]}])
    note = tasks._done_note(89, {"workspace": "booking"})
    assert "552931a" in note
    assert "Do not" in note and "re-implement" in note


def test_a_first_leg_with_nothing_committed_says_nothing(monkeypatch):
    monkeypatch.setattr(tasks, "committed_so_far", lambda tid, t: [])
    assert tasks._done_note(89, {"workspace": "booking"}) == ""


def test_a_capability_refusing_its_arguments_answers_the_brain(monkeypatch):
    """Not a server fault — an answer the model can act on.

    Task runs now carry Asta's MCP server, so a task can mis-call a capability
    for the first time. Eleven minutes after the restart that shipped it, one
    asked for workspace 'email'; the ValueError went unhandled, became a 500 and
    an ASGI traceback, and the brain saw a dead tool instead of "Unknown
    workspace 'email'. Registered: booking".
    """
    from fastapi.testclient import TestClient

    from app import capabilities, main as main_mod

    monkeypatch.setenv("ASTA_TOKEN", "qa-token")

    def _refuse(workspace: str = ""):
        raise ValueError(f"Unknown workspace '{workspace}'. Registered: booking")

    # Capability is frozen, so `fn` is set at construction, and the registry is
    # built once and cached — patch the built one rather than the table.
    probe = capabilities.Capability("qa_probe", "ops", fn=_refuse)
    monkeypatch.setitem(capabilities.registry(), "qa_probe", probe)

    r = TestClient(main_mod.app).post(
        "/api/_invoke", json={"tool": "qa_probe", "args": {"workspace": "email"}},
        headers={"Authorization": "Bearer qa-token"})
    assert r.status_code == 400                     # not 500
    assert "Registered: booking" in r.text          # and it says what to do instead


# --- "still waiting on you" for things that are not his -----------------------------------
#
# The screenshot of 2026-09-07 14:12, verbatim.

def test_a_message_addressed_to_one_colleague_by_first_name(monkeypatch):
    """"Priya Nair: Rahul lets take the billtoparty change tomorrow". The rule
    wanted TWO capitalised words, so a one-word address was invisible."""
    monkeypatch.setattr(chat_watch, "_people_he_talks_to",
                        lambda: ["Rahul Verma", "Alex Kumar"])
    assert chat_watch.names_someone_else(
        "Rahul lets take the billtoparty change tomorrow", "Priya Nair") is True


def test_a_capitalised_noun_is_not_a_name(monkeypatch):
    """The reason the one-word rule needs evidence rather than grammar: over-
    filtering loses him a real report."""
    monkeypatch.setattr(chat_watch, "_people_he_talks_to",
                        lambda: ["Rahul Verma", "Alex Kumar"])
    for text in ("Activity getting missed in prod", "Booking service is down",
                 "Production is failing for bulk upload"):
        assert chat_watch.names_someone_else(text, "Priya Nair") is False


def test_a_message_naming_him_is_still_his(monkeypatch):
    monkeypatch.setattr(chat_watch, "_people_he_talks_to", lambda: ["Alex Kumar"])
    assert chat_watch.names_someone_else("Arunkumar can you check this",
                                         "Priya Nair") is False


_POLL_TEXT = ("Poll: Names not recorded ; Results shared Do you plan to attend "
              "the team offsite from office tomorrow?")


def test_a_poll_is_addressed_to_the_room():
    """"Do you plan to attend the team offsite from office tomorrow?" is a
    question, so every ask-detector says yes — and it went to forty people."""
    assert attention.addressed_to_a_room("Polls", _POLL_TEXT) is True


def test_a_poll_posted_by_a_person_is_still_a_poll():
    """The sender is not always a bot: Teams attributes a poll to whoever created
    it. The text has to carry this on its own."""
    assert attention.addressed_to_a_room("Anita Rao", _POLL_TEXT) is True


def test_a_person_asking_about_a_poll_is_not_a_poll():
    assert attention.addressed_to_a_room(
        "Alex Kumar", "did you fill the form for tomorrow?") is False


def _owed_at(key: str, age_days: float, now: float) -> None:
    """One thing owed, first seen `age_days` ago and already past its due time —
    so the only thing that can exclude it is the age window."""
    seen = now - age_days * 86400
    attention.consider("teams-chat", key, who="Priya Nair",
                       what=f"{key} text", priority=1,
                       due_at=seen + 60, now=seen)


def test_a_two_day_old_ask_is_not_chased(monkeypatch):
    """The tomorrow it names has already been and gone."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    now = 1_800_000_000.0
    _owed_at("old1", 2.0, now)
    assert [r["key"] for r in delivery.chase_due(now=now)] == []


def test_something_from_this_morning_is_still_chased(monkeypatch):
    """The window must not become a way to drop live work — this is the case it
    would be easiest to break while fixing the one above."""
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    now = 1_800_000_000.0
    _owed_at("fresh1", 0.2, now)
    assert [r["key"] for r in delivery.chase_due(now=now)] == ["fresh1"]


def test_the_window_is_configurable(monkeypatch):
    monkeypatch.setenv("ASTA_CHASE_WINDOW_DAYS", "5")
    now = 1_800_000_000.0
    monkeypatch.setenv("ASTA_ATTENTION", "1")
    _owed_at("wide1", 2.0, now)
    assert [r["key"] for r in delivery.chase_due(now=now)] == ["wide1"]
