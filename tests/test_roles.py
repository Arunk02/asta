"""Which expert is answering — Round 3, and his own words for it.

"basically asta should act and change it and sustain to depend on the
situation": a senior developer when writing code, a debugging engineer when
something is broken, a site reliability engineer when it is production, a QA
engineer when it is testing, a solution architect when it is analysis and
planning, a reviewer when it is review.

The point is not a job title in a prompt. A title on its own changes nothing —
every brain already believes it is doing a good job. What a role is FOR is the
different first move: a debugger reproduces before it theorises, an SRE asks
what changed before it reads code, a reviewer looks for what the diff does not
say. So each role here is a short list of what that person does differently,
and the tests below hold that rather than the adjectives.

And one shared function, not a copy per brain: the 20-minute bug in
[[asta-consistency-across-brains]] came from two brains disagreeing about a
constant that should have had one home.
"""

from __future__ import annotations

import pytest

from app import roles


# --- reading the situation --------------------------------------------------------------

@pytest.mark.parametrize("said, expect", [
    # writing code
    ("implement the retry cap in the consumer", "coding"),
    ("add a flag to skip the callback", "coding"),
    ("refactor TmsServiceImpl, it's doing too much", "coding"),
    # something is broken and nobody knows why
    ("the booking service is throwing a null pointer, figure out why", "debugging"),
    ("this test passes locally and fails in CI, no idea", "debugging"),
    ("why is the retry logic running twice", "debugging"),
    # production, not the code
    ("preprod is returning 502s since the deploy", "infra"),
    ("the pod keeps restarting, OOMKilled", "infra"),
    ("error rate is up in telikos-prod since 2pm", "infra"),
    # proving it works
    ("write tests for the cancelled-booking path", "testing"),
    ("what test cases are we missing on the consumer", "testing"),
    # thinking before building
    ("how should we structure the new pricing service", "analysis"),
    ("compare using a queue vs calling the API directly", "analysis"),
    ("plan out the migration off the old scheduler", "analysis"),
    # judging somebody else's work
    ("review PR 1409", "review"),
    ("can you look over my changes before I merge", "review"),
])
def test_the_situation_picks_the_expert(said, expect):
    assert roles.role_for(said) == expect


def test_an_unreadable_request_gets_no_role_rather_than_a_guess():
    """A wrong expert is worse than none: an SRE brief on a coding task sends it
    looking at dashboards for a bug that is in the diff."""
    assert roles.role_for("hey") == ""
    assert roles.role_for("") == ""
    assert roles.brief("") == ""


@pytest.mark.parametrize("kind, expect", [
    ("review_request", "review"),
    ("pr_review", "review"),
    ("debug", "debugging"),
])
def test_a_kind_that_is_already_known_is_not_re_guessed(kind, expect):
    """The responder has already classified the message. Re-reading the text
    with a second classifier is how two parts of Asta come to disagree."""
    assert roles.role_for("whatever the text says", kind=kind) == expect


# --- what a role actually changes --------------------------------------------------------

def test_every_role_says_what_that_person_does_differently():
    for name in roles.ROLES:
        body = roles.brief(name)
        assert body.count("\n-") >= 3, f"{name} has no concrete moves"
        assert len(body) < 900, f"{name} is long enough to crowd out the task"


def test_the_debugger_reproduces_before_it_theorises():
    body = roles.brief("debugging").lower()
    assert "reproduce" in body and "evidence" in body


def test_the_site_engineer_asks_what_changed_before_reading_code():
    body = roles.brief("infra").lower()
    assert "what changed" in body and ("rollback" in body or "roll back" in body)


def test_the_qa_engineer_goes_after_the_paths_nobody_wrote_down():
    body = roles.brief("testing").lower()
    assert "edge" in body or "boundary" in body
    assert "fail" in body


def test_the_architect_gives_a_recommendation_not_a_menu():
    """His standing complaint about analysis: a list of options with no answer."""
    assert "recommend" in roles.brief("analysis").lower()


def test_the_reviewer_looks_for_what_the_diff_does_not_say():
    body = roles.brief("review").lower()
    assert "test" in body and ("missing" in body or "does not" in body)


def test_the_developer_is_told_to_match_the_code_around_it():
    assert "around it" in roles.brief("coding").lower()


# --- one home, every brain ---------------------------------------------------------------

def test_no_brain_carries_its_own_copy_of_this():
    """One shared function, or two brains drift apart on it — which is exactly
    the bug that took 20 minutes to find the last time."""
    import inspect

    from app import copilot_cli, responder, tasks
    for mod in (tasks, responder, copilot_cli):
        src = inspect.getsource(mod)
        assert "roles.brief" in src or "roles.role_for" in src, mod.__name__


def test_a_role_rides_along_with_a_code_task():
    body = tasks_brief("fix the null pointer in TmsServiceImpl")
    assert "debugging engineer" in body.lower() or "reproduce" in body.lower()


def tasks_brief(prompt: str) -> str:
    from app import tasks
    return tasks._with_pipeline("micro", prompt)


# --- and it stays put --------------------------------------------------------------------

def test_the_role_is_kept_for_the_whole_task_not_re_picked_each_turn():
    """"sustain" was his word. A task that starts as debugging stays debugging
    through "try again" and "what about the other repo", which on their own read
    as nothing in particular."""
    roles.remember("task-7", "debugging")
    assert roles.sustained("task-7", "try again") == "debugging"
    assert roles.sustained("task-7", "and the other repo too") == "debugging"


def test_a_clear_change_of_situation_still_changes_the_expert():
    """Sticky is not stuck: he moves from the fix to reviewing it in one thread."""
    roles.remember("task-8", "debugging")
    assert roles.sustained("task-8", "okay now review PR 1409 properly") == "review"


def test_nothing_remembered_reads_the_message_as_usual():
    assert roles.sustained("task-never-seen", "implement the retry cap") == "coding"
