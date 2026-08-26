"""Outward writes — the acts that cannot be quietly undone.

Every operation here is visible to someone else the moment it lands: a comment on
a colleague's ticket, an approval under Arun's name. The whole design rests on one
property, so it is what these tests are mostly about: what he approved is what
runs, byte for byte, with no model between the yes and the act.

The second theme is honesty on failure. A write that didn't happen, reported as
though it did, is worse than a write that never ran — he finds out from the
colleague who never got the comment.
"""

from __future__ import annotations

import pytest

from app import ops


@pytest.fixture
def spy(monkeypatch):
    """Capture what the underlying client was actually asked to do."""
    calls = {}

    async def add_comment(key, text):
        calls["comment"] = (key, text)
        return {"key": key, "comment_id": "1"}

    async def transition(key, to_status):
        calls["transition"] = (key, to_status)
        return {"key": key, "status": to_status}

    async def post_review(pr, workspace, repo, action, body):
        calls["review"] = (pr, workspace, repo, action, body)
        return f"Approved PR #{pr}"

    monkeypatch.setattr(ops.jira, "add_comment", add_comment)
    monkeypatch.setattr(ops.jira, "transition_issue", transition)
    monkeypatch.setattr(ops.review, "post_review", post_review)
    return calls


# --- the property the whole design rests on ---------------------------------

def test_the_recorded_arguments_are_what_run(spy):
    """Not "post a comment saying the migration is blocked" — THE comment. A brain
    re-reading its own instruction writes a different sentence every time, and the
    sentence he read is the only one he agreed to."""
    text = "Blocked on the schema review — Priya has the migration script."
    import asyncio
    asyncio.run(ops.run({"name": "jira_comment", "args": {"key": "PROJ-412", "text": text}}))
    assert spy["comment"] == ("PROJ-412", text)


def test_a_transition_carries_the_exact_target_status(spy):
    import asyncio
    asyncio.run(ops.run({"name": "jira_transition",
                         "args": {"key": "PROJ-9", "to_status": "Ready for Retest"}}))
    assert spy["transition"] == ("PROJ-9", "Ready for Retest")


def test_an_approval_names_the_pr_and_the_verb(spy):
    import asyncio
    out = asyncio.run(ops.run({"name": "pr_review",
                               "args": {"pr": "31", "workspace": "iom", "repo": "api",
                                        "action": "approve", "body": "LGTM"}}))
    assert spy["review"] == ("31", "iom", "api", "approve", "LGTM")
    assert "31" in out


# --- failure has to be loud -------------------------------------------------

def test_an_unknown_operation_raises_rather_than_doing_nothing():
    """A silent no-op after a yes is the worst outcome available: he believes it
    is done and nothing contradicts him."""
    import asyncio
    with pytest.raises(RuntimeError, match="unknown operation"):
        asyncio.run(ops.run({"name": "delete_everything", "args": {}}))


def test_an_op_with_no_name_is_refused_too():
    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(ops.run({}))


def test_the_underlying_error_is_allowed_to_surface(monkeypatch):
    async def boom(key, text):
        raise RuntimeError("Jira: 403 for /rest/api/3/issue/PROJ-1/comment")

    monkeypatch.setattr(ops.jira, "add_comment", boom)
    import asyncio
    with pytest.raises(RuntimeError, match="403"):
        asyncio.run(ops.run({"name": "jira_comment", "args": {"key": "PROJ-1", "text": "x"}}))


# --- what he reads before deciding ------------------------------------------

def test_describe_names_the_target_not_the_operation():
    assert ops.describe({"name": "jira_comment", "args": {"key": "PROJ-7"}}) == "Comment on PROJ-7"
    assert "PROJ-7" in ops.describe({"name": "jira_transition",
                                     "args": {"key": "PROJ-7", "to_status": "Done"}})


def test_describe_survives_a_malformed_op():
    """It appears in the message asking for his approval — it must never be the
    thing that stops him being asked."""
    assert ops.describe({"name": "jira_comment"})            # no args at all
    assert "unknown" in ops.describe({"name": "nope", "args": {}}).lower()
    assert ops.describe({})


def test_every_registered_op_is_async_and_documented():
    """A sync function registered here would return a coroutine-free value the
    dispatcher awaits — an error only reachable at approval time, which is the
    single worst moment to discover it."""
    import inspect
    assert ops.REGISTRY
    for name, entry in ops.REGISTRY.items():
        assert inspect.iscoroutinefunction(entry["run"]), name
        assert callable(entry["describe"]), name
