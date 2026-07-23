"""One registry of everything Asta can do.

Capabilities used to be taught three separate ways: as pydantic-ai tool
functions for chat, as hand-written curl instructions inside Copilot's first-turn
context, and as prose in the Claude CLI system prompt. Three descriptions of the
same tool, kept in sync by hand — and the memory note "changing
_first_turn_context requires clearing copilot_session:* rows" was the running
cost of that.

Now there is one table. A capability is declared once with its Python function;
the description is the function's own docstring, so it cannot drift. Every
consumer reads from here:

    chat (pydantic-ai)   tools_for(...)      -> the callables
    CLI brains           cli_block(port)     -> how to reach the same thing
    MCP server           registry()          -> name + description + callable
    tool selection       registry()          -> what to embed and rank

Adding a capability is one row plus a docstring, not a hunt through three files.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Callable

#: Capabilities that are ALWAYS in context, regardless of what the message is
#: about. Deliberately tiny — this is the set that lets the model recover when
#: retrieval picked wrong: it can remember, look something up, ask, or delegate.
ALWAYS = ("remember", "search_memory", "load_skill", "ask_user",
          "delegate_task", "list_background_tasks")


@dataclass(frozen=True)
class Capability:
    name: str
    group: str
    #: How a CLI brain reaches it over Asta's local API ("POST /api/tasks").
    http: str = ""
    #: How a CLI brain reaches it as a shell command, when there is no endpoint.
    shell: str = ""
    #: A hard rule that must travel with the capability everywhere it is taught.
    #: These are the rules that cost something when forgotten.
    note: str = ""
    #: True when calling it changes something outside Asta (sends, posts, pushes).
    write: bool = False
    fn: Callable | None = field(default=None, compare=False)

    @property
    def description(self) -> str:
        """The function's own docstring — the single description of this tool."""
        return inspect.cleandoc(self.fn.__doc__ or "") if self.fn else ""

    @property
    def summary(self) -> str:
        """First line only, for indexes and rankings."""
        return self.description.split("\n\n")[0].replace("\n", " ").strip()


# The table. `fn` is filled in from app.agent at first use — declaring it here
# would make this module import agent, and agent imports this one.
_TABLE: tuple[Capability, ...] = (
    # --- memory -------------------------------------------------------------
    Capability("remember", "memory"),
    Capability("search_memory", "memory"),
    Capability("load_skill", "memory"),
    # --- asking -------------------------------------------------------------
    Capability("ask_user", "ask", http="POST /api/ask",
               note="One question, pushed to Arun's phone; the caller blocks until he "
                    "answers. Cheaper than stopping a whole pipeline for a re-plan."),
    # --- workspace / code context -------------------------------------------
    Capability("resolve_context", "workspace",
               http="GET /api/workspaces/{workspace}/resolve?q={question}",
               note="ALWAYS resolve before reading code. Never explore a repo blindly."),
    Capability("read_workspace_file", "workspace",
               http="GET /api/workspaces/{workspace}/file?path={rel_path}"),
    Capability("list_services", "workspace"),
    Capability("refresh_context", "workspace", http="POST /api/refresh/{workspace}"),
    # --- jira ----------------------------------------------------------------
    Capability("jira_search", "jira", http="GET /api/jira/search?jql={urlencoded}"),
    Capability("jira_my_issues", "jira",
               http="GET /api/jira/search?jql=assignee = currentUser() AND "
                    "statusCategory != Done ORDER BY updated DESC"),
    Capability("jira_issue", "jira", http="GET /api/jira/issue/{key}"),
    Capability("jira_comment", "jira", http='POST /api/jira/issue/{key}/comment {"text":"…"}',
               write=True,
               note="WRITE: show Arun the exact text and get his confirmation first, "
                    "unless he dictated it in the same message."),
    Capability("jira_transition", "jira",
               http='GET /api/jira/issue/{key}/transitions to list, then '
                    'POST /api/jira/issue/{key}/transition {"status":"<name>"}',
               write=True,
               note="WRITE: confirm the target status with Arun first."),
    # --- teams / outlook (browser automation, no endpoint) -------------------
    Capability("teams_activity", "teams",
               shell="python -m app.teams_bridge activity [limit]",
               note="USE THIS for 'any messages for me', 'anything from X', 'what did I "
                    "miss', 'any mentions' — it reads Teams itself, so muted chats count."),
    Capability("teams_read_chat", "teams",
               shell='python -m app.teams_bridge read "<chat name>" [limit]'),
    Capability("teams_send_message", "teams",
               shell='python -m app.teams_bridge send "<chat name>" "<text>"',
               write=True,
               note="HARD RULE: a person's name targets their ONE-TO-ONE chat. NEVER a "
                    "group chat or team channel unless Arun names the group himself (then "
                    "add --group). A 1:1 message once landed in a team channel — never "
                    "again. Repeat the 'sent to: <chat>' line back to Arun; if it printed "
                    "ERROR, say it did NOT send."),
    Capability("outlook_mail", "outlook",
               shell="python -m app.outlook mail [limit]  ·  python -m app.outlook attention"),
    Capability("outlook_meetings", "outlook", shell="python -m app.outlook meetings"),
    # --- the work engine -----------------------------------------------------
    Capability("delegate_task", "tasks",
               http='POST /api/tasks {"title":"…","prompt":"full SELF-CONTAINED '
                    'instructions","kind":"analysis|code|teams_draft","workspace":""}',
               note="The ONLY way work gets done. kinds: analysis (read-only, parallel) | "
                    "code (edits a repo; set workspace; one per workspace at a time) | "
                    'teams_draft (also set "teams_chat"; always waits for approval). '
                    'Optional "executor":"claude"|"copilot" — set only when Arun names one. '
                    "Reply to Arun with the task id immediately; do NOT wait for it."),
    Capability("review_pr", "tasks", http='POST /api/review {"pr":"123","workspace":"…","repo":""}',
               note="Produces notes for ARUN to post. Never comment on or approve a PR "
                    "yourself — the review is his to give."),
    Capability("list_background_tasks", "tasks", http="GET /api/tasks"),
    Capability("task_result", "tasks", http="GET /api/tasks/{id}"),
    Capability("approve_task", "tasks", http="POST /api/tasks/{id}/approve", write=True,
               note="At a plan gate this means implement. Any other feedback goes to "
                    'POST /api/tasks/{id}/reply {"text":"…"} and the pipeline re-plans.'),
    Capability("ship_task", "tasks", http="POST /api/tasks/{id}/ship", write=True,
               note="Pushes the branch and opens the PR. The pipeline NEVER does this "
                    "itself — only when Arun has seen the diff and said ship."),
    Capability("reject_task", "tasks", http="POST /api/tasks/{id}/reject", write=True),
    # --- rhythm / ops --------------------------------------------------------
    Capability("set_reminder", "reminders",
               http='POST /api/reminders {"text":"…","due":"<LOCAL ISO>","repeat":""}',
               note="due is LOCAL ISO time you compute yourself; repeat: ''|daily|weekdays|weekly."),
    Capability("list_my_reminders", "reminders", http="GET /api/reminders"),
    Capability("cancel_reminder", "reminders", http="POST /api/reminders/{id}/cancel"),
    Capability("morning_brief", "rhythm", http="POST /api/brief/now",
               note="Auto-runs on weekday mornings; trigger only when Arun asks."),
    Capability("standup_draft", "rhythm", http="POST /api/standup/now"),
    Capability("health_check", "ops", http="GET /api/health"),
    Capability("ci_status", "ops", http="GET /api/ci"),
    Capability("trace_report", "ops", http="GET /api/traces"),
    Capability("quality_report", "ops", http="GET /api/quality",
               note="The evidence for 'is Asta getting better'. Cite it rather than "
                    "asserting an improvement."),
)

_REGISTRY: dict[str, Capability] | None = None


def registry() -> dict[str, Capability]:
    """name -> Capability, with functions bound. Built once."""
    global _REGISTRY
    if _REGISTRY is None:
        from . import agent as agent_mod
        out: dict[str, Capability] = {}
        for cap in _TABLE:
            fn = getattr(agent_mod, cap.name, None)
            if fn is None:
                # A row with no function is a bug, not a soft failure: every
                # consumer below would silently teach a tool that cannot be called.
                raise RuntimeError(f"capability '{cap.name}' has no function in app.agent")
            out[cap.name] = Capability(**{**cap.__dict__, "fn": fn})
        _REGISTRY = out
    return _REGISTRY


def get(name: str) -> Capability | None:
    return registry().get(name)


def names() -> tuple[str, ...]:
    return tuple(registry())


def tools_for(selected: list[str] | tuple[str, ...] | None = None) -> list[Callable]:
    """The callables to hand pydantic-ai. None = everything (the old behaviour)."""
    reg = registry()
    if selected is None:
        return [c.fn for c in reg.values()]
    keep = list(dict.fromkeys(list(selected) + [n for n in ALWAYS if n in reg]))
    return [reg[n].fn for n in keep if n in reg]


def notes_block(selected: list[str] | tuple[str, ...] | None = None) -> str:
    """The hard rules for the selected capabilities.

    These ride separately from the tool descriptions because they are the
    expensive ones to forget — a message in the wrong Teams chat, a PR opened
    unprompted. When retrieval narrows the toolset, the rules narrow with it.
    """
    reg = registry()
    chosen = reg.values() if selected is None else [reg[n] for n in selected if n in reg]
    lines = [f"- {c.name}: {c.note}" for c in chosen if c.note]
    return "## Rules that apply to these tools\n" + "\n".join(lines) if lines else ""


def cli_block(port: str = "8321", root: str = "") -> str:
    """How a CLI brain reaches these same capabilities.

    Generated from the table, so a new tool is taught to Copilot and Claude the
    moment it is added to chat — no second description to update, and no stale
    copilot session to clear.
    """
    reg = registry()
    http = [c for c in reg.values() if c.http]
    shell = [c for c in reg.values() if c.shell]
    parts: list[str] = []
    if http:
        rows = "\n".join(f"  {c.name} — {c.http}\n      {c.summary}" for c in http)
        parts.append(
            f"Asta's own API (http://127.0.0.1:{port}, every call needs "
            '-H "Authorization: Bearer $ASTA_TOKEN"; POST bodies are JSON):\n' + rows)
    if shell:
        prefix = f"{root}/.venv/bin/" if root else ""
        rows = "\n".join(
            f"  {c.name} — {prefix}{c.shell}\n      {c.summary}" for c in shell)
        parts.append(
            "Shell capabilities (browser automation on Arun's logged-in session, "
            "~10-25s each):\n" + rows)
    rules = [f"- {c.name}: {c.note}" for c in reg.values() if c.note]
    if rules:
        parts.append("RULES (these are the expensive ones to get wrong):\n" + "\n".join(rules))
    return "\n\n".join(parts)


def index_block(selected: list[str] | tuple[str, ...] | None = None) -> str:
    """One line per capability — for a brain that has the names but not the schemas."""
    reg = registry()
    chosen = reg.values() if selected is None else [reg[n] for n in selected if n in reg]
    return "\n".join(f"- **{c.name}** ({c.group}) — {c.summary}" for c in chosen)
