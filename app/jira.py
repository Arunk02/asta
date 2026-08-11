"""Jira: REST v3 client (read + comment + transition) + change watcher.

This REST path is the PRIMARY Jira integration (works with every brain incl.
Copilot CLI, powers the zero-token watcher/brief). The Atlassian MCP server is
the optional fallback for anything not covered here (e.g. Confluence).

Configure in .env:
  JIRA_BASE_URL=https://yourcompany.atlassian.net
  JIRA_EMAIL=you@company.com
  JIRA_API_TOKEN=...        (create at id.atlassian.com → API tokens)
  JIRA_WATCH_JQL=assignee = currentUser() AND statusCategory != Done
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from . import store

#: How much of a comment thread to carry by default. Ten is enough to hold the
#: conversation that settled the requirement without turning a routine lookup
#: into a wall of "+1" and automation noise.
COMMENT_LIMIT = 10
#: Per-comment cap. Long ones are usually a pasted stack trace or a spec dump;
#: the first part carries the point.
COMMENT_CHARS = 1500


def configured() -> bool:
    return bool(os.environ.get("JIRA_BASE_URL") and os.environ.get("JIRA_API_TOKEN"))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=os.environ["JIRA_BASE_URL"].rstrip("/"),
        auth=(os.environ.get("JIRA_EMAIL", ""), os.environ["JIRA_API_TOKEN"]),
        timeout=20,
        headers={"Accept": "application/json"},
    )


def _fmt_issue(i: dict) -> dict:
    f = i.get("fields", {})
    return {
        "key": i.get("key"),
        "summary": f.get("summary"),
        "status": (f.get("status") or {}).get("name"),
        "assignee": ((f.get("assignee") or {}).get("displayName")),
        "priority": (f.get("priority") or {}).get("name"),
        "updated": f.get("updated"),
        "type": (f.get("issuetype") or {}).get("name"),
    }


async def search(jql: str, limit: int = 15) -> list[dict]:
    if not configured():
        raise RuntimeError("Jira is not configured — set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env")
    async with _client() as c:
        r = await c.get("/rest/api/3/search/jql", params={
            "jql": jql, "maxResults": limit,
            "fields": "summary,status,assignee,priority,updated,issuetype",
        })
        if r.status_code == 404:  # older deployments
            r = await c.get("/rest/api/3/search", params={
                "jql": jql, "maxResults": limit,
                "fields": "summary,status,assignee,priority,updated,issuetype",
            })
        r.raise_for_status()
        return [_fmt_issue(i) for i in r.json().get("issues", [])]


def sprint_jql() -> str:
    """What "the current sprint" means here.

    JQL rather than the Agile REST API on purpose: `openSprints()` needs no board
    id, so this works on a fresh install with nothing configured beyond the
    credentials. Instances without Jira Software have no sprint field at all —
    `current_sprint` turns that error into a sentence instead of a stack trace.
    """
    return os.environ.get(
        "JIRA_SPRINT_JQL",
        "sprint in openSprints() AND assignee = currentUser() ORDER BY status ASC")


async def current_sprint(limit: int = 30) -> list[dict]:
    """Everything assigned to him in the open sprint — the board, not the backlog.

    Distinct from `JIRA_WATCH_JQL`, which is "assigned and not done" and happily
    includes work from three sprints ago. Standup and "what's on me this sprint"
    want the committed set.
    """
    try:
        return await search(sprint_jql(), limit=limit)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            raise RuntimeError(
                "This Jira project has no sprints (or the JQL is not valid here) — "
                "set JIRA_SPRINT_JQL in .env to whatever 'current work' means for "
                "your board.") from exc
        raise


def _adf_to_text(node) -> str:
    """Flatten Atlassian Document Format to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            out.append(_adf_to_text(child))
        if node.get("type") in ("paragraph", "heading", "listItem"):
            out.append("\n")
    elif isinstance(node, list):
        out += [_adf_to_text(n) for n in node]
    return "".join(out)


def _fmt_comment(cm: dict) -> dict:
    # A comment whose body is only an image, an attachment or an embedded card
    # has no text nodes at all, and flattens to "". Rendered raw that becomes a
    # bare author name with nothing after it, which reads as a comment someone
    # left blank rather than as content that did not survive the flattening.
    text = _adf_to_text(cm.get("body")).strip()[:COMMENT_CHARS]
    return {
        "author": (cm.get("author") or {}).get("displayName", "someone"),
        "text": text or "(no text — image, attachment or embedded card)",
        "created": cm.get("created", ""),
    }


async def _fetch_comments(c: httpx.AsyncClient, key: str, limit: int) -> dict:
    """The newest `limit` comments, oldest-first, plus how many exist in all.

    Deliberately NOT the `comment` field of the issue payload, which is where
    this used to come from. That field pages from the START: ask a ticket with
    forty comments for its comments and Jira hands back the first twenty — the
    original triage chatter — and silently omits the decision someone made
    yesterday. Sorting by `-created` asks for the end of the thread instead.

    Returned oldest-first even though they arrive newest-first, because a
    conversation read backwards is a conversation misread.
    """
    r = await c.get(f"/rest/api/3/issue/{key}/comment",
                    params={"orderBy": "-created", "maxResults": max(1, limit)})
    r.raise_for_status()
    data = r.json()
    items = [_fmt_comment(cm) for cm in data.get("comments", [])]
    items.reverse()
    return {"items": items, "total": int(data.get("total", len(items)))}


async def comments(key: str, limit: int = COMMENT_LIMIT) -> dict:
    """Public comment read: {"items": [{author, text, created}], "total": int}."""
    _require_configured()
    async with _client() as c:
        return await _fetch_comments(c, key, limit)


async def get_issue(key: str, comment_limit: int = COMMENT_LIMIT) -> dict:
    if not configured():
        raise RuntimeError("Jira is not configured — set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env")
    async with _client() as c:
        # Concurrent: the comment thread is a second round trip now that it no
        # longer rides along with the issue, and there is no reason to pay for
        # it serially.
        issue_req = c.get(f"/rest/api/3/issue/{key}", params={
            "fields": "summary,status,assignee,priority,updated,issuetype,"
                      "description,labels,components",
        })
        r, thread = await asyncio.gather(issue_req, _fetch_comments(c, key, comment_limit))
        r.raise_for_status()
        data = r.json()
    issue = _fmt_issue(data)
    f = data.get("fields", {})
    issue["description"] = _adf_to_text(f.get("description"))[:6000]
    issue["labels"] = f.get("labels", [])
    issue["components"] = [c.get("name") for c in f.get("components", [])]
    # Comments carry the real requirements on many tickets — the acceptance
    # criteria live in the Q&A between reporter and dev, not in the one-line
    # description. `comment_total` travels with them so a caller can tell a
    # complete thread from a truncated one instead of assuming it saw everything.
    issue["comments"] = thread["items"]
    issue["comment_total"] = thread["total"]
    return issue


async def latest_comment(key: str) -> dict | None:
    """Newest comment on an issue as {author, text, created}, or None."""
    if not configured():
        return None
    async with _client() as c:
        thread = await _fetch_comments(c, key, 1)
    return thread["items"][-1] if thread["items"] else None


def _require_configured() -> None:
    if not configured():
        raise RuntimeError("Jira is not configured — set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env")


async def add_comment(key: str, text: str) -> dict:
    """Post a plain-text comment; returns {key, comment_id}."""
    _require_configured()
    body = {"body": {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]},
    ]}}
    async with _client() as c:
        r = await c.post(f"/rest/api/3/issue/{key}/comment", json=body)
        r.raise_for_status()
        return {"key": key, "comment_id": r.json().get("id")}


async def list_transitions(key: str) -> list[str]:
    """Status names this issue can move to right now (workflow-dependent)."""
    _require_configured()
    async with _client() as c:
        r = await c.get(f"/rest/api/3/issue/{key}/transitions")
        r.raise_for_status()
        return [t.get("name", "") for t in r.json().get("transitions", [])]


async def transition_issue(key: str, to_status: str) -> dict:
    """Move an issue to a status by name (case-insensitive). Raises with the
    list of valid targets if the name doesn't match the workflow."""
    _require_configured()
    async with _client() as c:
        r = await c.get(f"/rest/api/3/issue/{key}/transitions")
        r.raise_for_status()
        transitions = r.json().get("transitions", [])
        match = next((t for t in transitions if t.get("name", "").lower() == to_status.strip().lower()), None)
        if match is None:
            names = ", ".join(t.get("name", "?") for t in transitions) or "none"
            raise RuntimeError(f"{key} cannot move to '{to_status}' — valid targets: {names}")
        r = await c.post(f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": match["id"]}})
        r.raise_for_status()
    return {"key": key, "status": match["name"]}


async def check_for_changes() -> list[str]:
    """Poll the watch JQL; return human-readable change lines since the last poll."""
    if not configured():
        return []
    jql = os.environ.get("JIRA_WATCH_JQL", "assignee = currentUser() AND statusCategory != Done")
    issues = await search(jql, limit=30)
    prev_raw = store.kv_get("jira_watch_state")
    prev = json.loads(prev_raw) if prev_raw else {}
    current = {i["key"]: {"updated": i["updated"], "status": i["status"], "summary": i["summary"]} for i in issues}
    changes = []
    if prev:  # first run just sets the watermark
        for key, cur in current.items():
            old = prev.get(key)
            if old is None:
                changes.append(f"🆕 {key} assigned/new: {cur['summary']} [{cur['status']}]")
            elif old["updated"] != cur["updated"]:
                if old["status"] != cur["status"]:
                    changes.append(f"✏️ {key} status {old['status']} → {cur['status']}: {cur['summary']}")
                    continue
                # No status move: the usual cause is a comment. Naming the author
                # and quoting them is the difference between a useful ping and
                # "something changed, go look".
                line = f"✏️ {key} updated: {cur['summary']}"
                try:
                    cm = await latest_comment(key)
                    if cm and cm["created"] > (old.get("updated") or ""):
                        snippet = " ".join(cm["text"].split())[:180]
                        line = f"💬 {key} — {cm['author']} commented: “{snippet}” ({cur['summary']})"
                except Exception:
                    pass
                changes.append(line)
    store.kv_set("jira_watch_state", json.dumps(current))
    return changes
