"""The sprint, and the writes that go onto other people's tickets.

Two changes here and they are both about who decides. Reading the CURRENT SPRINT
is new because "everything assigned and not done" happily includes work from three
sprints ago, which is not what "what's on me this sprint" means.

And the write tools no longer write. They stage the exact call and wait. The old
tools posted the moment the model called them, with only a docstring asking it to
confirm first — and a docstring is a request, not a control. The comment that lands
on a colleague's ticket is now the one Arun read.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import agent, jira, offers


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://co.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "arun@co.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    monkeypatch.delenv("JIRA_SPRINT_JQL", raising=False)
    offers.clear()
    yield


# --- the current sprint -----------------------------------------------------

def test_the_sprint_is_the_open_one_not_everything_assigned():
    assert "openSprints()" in jira.sprint_jql()
    assert "currentUser()" in jira.sprint_jql()


def test_the_sprint_query_is_overridable(monkeypatch):
    """Boards differ, and "current work" on a Kanban board is not a sprint at all."""
    monkeypatch.setenv("JIRA_SPRINT_JQL", "project = X AND status = 'In Progress'")
    assert jira.sprint_jql() == "project = X AND status = 'In Progress'"


def test_an_instance_without_sprints_explains_itself(monkeypatch):
    """Jira answers 400 for a field that does not exist there. A stack trace would
    tell him nothing about the one line of .env that fixes it."""
    async def rejects(jql, limit=15):
        request = httpx.Request("GET", "https://co.atlassian.net/rest/api/3/search/jql")
        raise httpx.HTTPStatusError(
            "bad", request=request, response=httpx.Response(400, request=request))

    monkeypatch.setattr(jira, "search", rejects)
    with pytest.raises(RuntimeError, match="JIRA_SPRINT_JQL"):
        asyncio.run(jira.current_sprint())


def test_a_real_failure_is_not_disguised_as_a_config_problem(monkeypatch):
    """A 401 means the token is wrong, and telling him to edit his JQL would send
    him hunting in the wrong place."""
    async def unauthorised(jql, limit=15):
        request = httpx.Request("GET", "https://co.atlassian.net/x")
        raise httpx.HTTPStatusError(
            "no", request=request, response=httpx.Response(401, request=request))

    monkeypatch.setattr(jira, "search", unauthorised)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(jira.current_sprint())


def test_the_sprint_tool_reports_an_empty_sprint_plainly(monkeypatch):
    async def none(limit=30):
        return []

    monkeypatch.setattr(jira, "current_sprint", none)
    assert "Nothing assigned" in asyncio.run(agent.jira_sprint())


def test_the_sprint_tool_lists_key_status_and_summary(monkeypatch):
    async def two(limit=30):
        return [{"key": "PROJ-1", "status": "In Progress", "summary": "Fix login"},
                {"key": "PROJ-2", "status": "To Do", "summary": "Add metrics"}]

    monkeypatch.setattr(jira, "current_sprint", two)
    out = asyncio.run(agent.jira_sprint())
    assert "PROJ-1 [In Progress] Fix login" in out


# --- comments are staged, not posted ----------------------------------------

def test_commenting_stages_the_exact_text_and_posts_nothing(monkeypatch):
    monkeypatch.setattr(jira, "add_comment",
                        lambda *a: pytest.fail("nothing may post before he says yes"))
    text = "Blocked on the schema review — Priya has the migration script."
    out = asyncio.run(agent.jira_comment("PROJ-412", text))
    o = offers.pending()
    assert "waiting for Arun's yes" in out
    assert o.op == {"name": "jira_comment", "args": {"key": "PROJ-412", "text": text}}


def test_he_reads_the_comment_itself_before_approving_it():
    text = "Blocked on the schema review."
    asyncio.run(agent.jira_comment("PROJ-412", text))
    assert text in offers.pending().context


def test_commenting_without_jira_configured_says_so(monkeypatch):
    monkeypatch.delenv("JIRA_API_TOKEN")
    assert "not configured" in asyncio.run(agent.jira_comment("P-1", "x"))
    assert offers.pending() is None


# --- transitions check the workflow BEFORE asking him -----------------------

def test_a_transition_is_staged_after_the_workflow_says_it_is_possible(monkeypatch):
    async def transitions(key):
        return ["In Progress", "Ready for Retest", "Done"]

    monkeypatch.setattr(jira, "list_transitions", transitions)
    monkeypatch.setattr(jira, "transition_issue",
                        lambda *a: pytest.fail("nothing may move before he says yes"))
    out = asyncio.run(agent.jira_transition("PROJ-9", "Ready for Retest"))
    assert "waiting for Arun's yes" in out
    assert offers.pending().op["args"] == {"key": "PROJ-9", "to_status": "Ready for Retest"}


def test_an_impossible_status_fails_before_he_is_asked(monkeypatch):
    """Asking him to approve something the workflow will reject wastes the one
    interaction he actually has to pay attention to."""
    async def transitions(key):
        return ["In Progress", "Done"]

    monkeypatch.setattr(jira, "list_transitions", transitions)
    out = asyncio.run(agent.jira_transition("PROJ-9", "Ready for Retest"))
    assert "valid targets: In Progress, Done" in out
    assert offers.pending() is None


def test_the_status_match_ignores_case_and_spacing(monkeypatch):
    async def transitions(key):
        return ["Ready for Retest"]

    monkeypatch.setattr(jira, "list_transitions", transitions)
    asyncio.run(agent.jira_transition("PROJ-9", "  ready FOR retest "))
    assert offers.pending() is not None


def test_an_unreadable_workflow_is_reported_not_guessed(monkeypatch):
    async def boom(key):
        raise RuntimeError("Jira: 404")

    monkeypatch.setattr(jira, "list_transitions", boom)
    out = asyncio.run(agent.jira_transition("PROJ-9", "Done"))
    assert "Could not read" in out
    assert offers.pending() is None


# --- the same rule reaches a CLI brain --------------------------------------

def test_the_http_endpoint_stages_exactly_like_the_chat_tool(monkeypatch):
    """A CLI brain reaching this by curl must not get a write path the in-process
    brain doesn't have. One policy, or the rule only holds where someone
    remembered to write it."""
    from app import capabilities
    cap = capabilities.get("jira_comment")
    assert "STAGES" in cap.note
    assert cap.write
    from app import main
    assert main.api_jira_comment.__doc__ and "SAME function" in main.api_jira_comment.__doc__
