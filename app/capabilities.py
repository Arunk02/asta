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
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable

#: Capabilities that are ALWAYS in context, regardless of what the message is
#: about. Deliberately tiny — this is the set that lets the model recover when
#: retrieval picked wrong: it can remember, look something up, ask, or delegate.
#: Exposed on every turn no matter what the ranker picked.
#:
#: `reject_task` is here for one reason. On 27 August a code task was spawned that
#: Arun had not asked for, and when he said to stop it the ranker had not selected
#: the tool that stops it — so he was told "no cancel/stop tool is available to me
#: … it will push when done unless you intervene directly". That was false;
#: `tasks.cancel` kills the worker. A capability that can start irreversible work
#: on every turn must be matched by the one that stops it on every turn, or the
#: floor guarantees a start it cannot take back.
ALWAYS = ("remember", "search_memory", "load_skill", "ask_user",
          "delegate_task", "list_background_tasks", "reject_task",
          "continue_working", "prepare_to_send")


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
    Capability("remember", "memory",
               http='POST /api/memory {"title":"…","fact":"…","kind":"fact"}',
               note="Durable memory across ALL conversations. kind: fact | preference "
                    "| gotcha | fix. Save corrections and preferences, not chatter."),
    Capability("search_memory", "memory", http="GET /api/memory/search?q={query}"),
    Capability("load_skill", "memory", http="GET /api/skills/{name}"),
    # --- asking -------------------------------------------------------------
    Capability("ask_user", "ask", http="POST /api/ask",
               note="One question, pushed to Arun's phone; the caller blocks until he "
                    "answers. Cheaper than stopping a whole pipeline for a re-plan."),
    # --- autonomous loop -----------------------------------------------------
    Capability("continue_working", "loop",
               http='POST /api/loop/continue {"next_step":"…"}',
               note="Call as your LAST action when the task isn't done and you know the "
                    "next step — Asta runs it without waiting for Arun. Not for sending "
                    "anything outward; stop instead when the work is actually finished."),
    Capability("propose_next", "loop",
               http='POST /api/propose-next {"next_step":"…","why":"…"}',
               note="How ANY flow continues past one turn: name the next move concretely "
                    "and stop. His yes runs it, from any channel, hours later if need be. "
                    "Use it instead of asking 'shall I?' in prose — prose is lost when the "
                    "turn ends. Not for anything leaving the chat: that is prepare_to_send."),
    Capability("prepare_to_send", "loop",
               http='POST /api/loop/prepare-send {"what":"…","to":"…","channel":"teams|email|jira|pr|chat"}',
               note="The ONLY approved way to send on Arun's behalf: it STAGES the draft "
                    "and asks him to confirm — nothing goes out until he says yes. Never "
                    "send outward through any other tool without staging it here first."),
    # --- workspace / code context -------------------------------------------
    Capability("resolve_context", "workspace",
               http="GET /api/workspaces/{workspace}/resolve?q={question}",
               note="ALWAYS resolve before reading code. Never explore a repo blindly."),
    Capability("read_workspace_file", "workspace",
               http="GET /api/workspaces/{workspace}/file?path={rel_path}",
               note="resolve_context FIRST — it routes you to the ~350 tokens that matter. "
                    "Reading files blind loads whole classes into context and is the #1 "
                    "avoidable token sink (the auditor flags it as BLIND_READ)."),
    Capability("list_services", "workspace",
               http="GET /api/workspaces/{workspace}/services"),
    Capability("refresh_context", "workspace", http="POST /api/refresh/{workspace}"),
    # --- jira ----------------------------------------------------------------
    Capability("jira_search", "jira", http="GET /api/jira/search?jql={urlencoded}"),
    Capability("jira_my_issues", "jira",
               http="GET /api/jira/search?jql=assignee = currentUser() AND "
                    "statusCategory != Done ORDER BY updated DESC"),
    Capability("jira_issue", "jira", http="GET /api/jira/issue/{key}[?comments=N]",
               note="Returns the description AND the comment thread. Read the comments "
                    "before answering: on many tickets the description is one line and "
                    "the real requirement was settled in the Q&A under it. If the ticket "
                    "still doesn't explain itself, ask Arun — do not infer it from the title."),
    Capability("jira_sprint", "jira", http="GET /api/jira/sprint",
               note="The CURRENT sprint, not everything assigned — use this for 'what's on "
                    "me this sprint', standup, and before offering to pick work up."),
    Capability("jira_comment", "jira", http='POST /api/jira/issue/{key}/comment {"text":"…"}',
               write=True,
               note="STAGES, does not post. Write the EXACT finished comment — Arun's yes "
                    "posts those words unchanged, so it must read as the comment itself, "
                    "not a description of one. Tell him it's waiting."),
    Capability("jira_transition", "jira",
               http='GET /api/jira/issue/{key}/transitions to list, then '
                    'POST /api/jira/issue/{key}/transition {"status":"<name>"}',
               write=True,
               note="STAGES, does not move it. An unreachable status fails immediately "
                    "with the valid targets — offer those to Arun rather than guessing."),
    # --- teams / outlook (browser automation, no endpoint) -------------------
    Capability("teams_activity", "teams",
               shell="python -m app.teams_bridge activity [limit]",
               note="USE THIS for 'any messages for me', 'anything from X', 'what did I "
                    "miss', 'any mentions' — it reads Teams itself, so muted chats count."),
    Capability("teams_read_chat", "teams",
               shell='python -m app.teams_bridge read "<chat name>" [limit]'),
    Capability("draft_voice", "teams", http="GET /api/voice?person={person}",
               note="Call BEFORE drafting any Teams/WhatsApp message to a person. "
                    "Returns how Arun writes to THAT person. Terms of address belong "
                    "to a relationship, not to him — 'bro' is attested with one "
                    "colleague only, and it is stripped automatically for anyone "
                    "else, so never assume one fits."),
    Capability("teams_search", "teams",
               http="GET /api/teams/search?q={query}",
               shell='python -m app.teams_bridge search "<topic>"',
               note="Searches only what Asta has ALREADY read — its own record, not all "
                    "of Teams. Say so when nothing matches: 'I have no record of that' is "
                    "true, 'nobody said that' is not. For something that may never have "
                    "been read, teams_history goes and fetches it."),
    Capability("teams_history", "teams",
               shell='python -m app.teams_bridge history "<chat name>" "<when>"',
               note="USE THIS, not teams_read_chat, whenever the question has a WHEN in "
                    "it — 'last night', 'yesterday', 'this morning', 'while I was away'. "
                    "teams_read_chat only sees what is on screen now; Teams drops older "
                    "messages out of the DOM, so it CANNOT answer about last night and "
                    "will quietly return today's messages instead."),
    Capability("teams_unread", "teams", http="GET /api/teams/unread",
               note="Unread chats from the RAIL — 1:1 and group, tagged or not. The "
                    "Activity feed only carries mentions/replies, so ordinary "
                    "messages and untagged follow-ups never appear there."),
    Capability("teams_resolve", "teams",
               shell='python -m app.teams_bridge resolve "<name>" [--group]',
               note="Checks WHO a message would reach without sending. Use it before "
                    "sending to a short or common name, and for any group. An ambiguous "
                    "name is refused here rather than delivered to the wrong person."),
    Capability("teams_call", "teams", write=True,
               shell='python -m app.teams_bridge call "<person>" [--video]',
               note="DIALS when Arun asked for a call in his own words — asking IS "
                    "the consent, do not stage it back to him. Stages only when the "
                    "call is Asta's idea. Never answer 'I can't call' — say which "
                    "part failed."),
    Capability("discuss_in_call", "teams", write=True,
               http='POST /api/meetings/discuss {"who":"…","topic":"…"}',
               note="Rings them AND holds the conversation — listens, answers, hangs "
                    "up. This is the tool for 'call X and discuss Y'. Never say Asta "
                    "cannot hold a live conversation; it can. It commits Arun to "
                    "nothing — an unknown becomes 'I'll check with Arun'."),
    Capability("teams_send_message", "teams",
               shell='python -m app.teams_bridge send "<chat name>" "<text>"',
               write=True,
               note="HARD RULE: a person's name targets their ONE-TO-ONE chat. NEVER a "
                    "group chat or team channel unless Arun names the group himself (then "
                    "add --group). A 1:1 message once landed in a team channel — never "
                    "again. Repeat the 'sent to: <chat>' line back to Arun; if it printed "
                    "ERROR, say it did NOT send."),
    Capability("draft_teams_reply", "teams",
               http="GET /api/teams-draft?chat={chat}&question={question}",
               note="DRAFT only — read + draft an answer to a person's Teams question. To "
                    "actually reply, stage it with prepare_to_send (channel teams); never "
                    "sends in Arun's name unprompted."),
    Capability("teams_status", "teams", http="GET /api/teams/presence to read, "
                                            'POST /api/teams/presence {"status":"dnd"} to set',
               write=True,
               note="His OWN status, so just do it when he asks — but report what it reads "
                    "back afterwards. A DND he thinks is set and isn't costs him the hour."),
    Capability("join_meeting", "teams",
               http='POST /api/meetings/join {"join_url":"…","title":""}',
               write=True,
               note="Joins MUTED with the camera off, always. Joining is listening only; "
                    "say so, and never imply anything was said on his behalf. It hangs up "
                    "by itself when the call ends — reply to Arun immediately, don't wait."),
    Capability("join_meeting_by_name", "teams",
               http='POST /api/meetings/join {"which":"my 3pm"}',
               write=True,
               note="The one to reach for when he NAMES a meeting rather than pasting a "
                    "link — 'join my 3pm', 'join the standup'. Refuses and lists the day "
                    "when the phrase fits more than one; hand that back and ask which. "
                    "Joining the wrong call cannot be quietly undone."),
    Capability("answer_call", "teams", write=True,
               http='POST /api/calls/answer {"speak":false}',
               note="Picks up a RINGING call. Only after Arun said yes — answering "
                    "puts Asta in front of someone who thinks they reached him. "
                    "speak=true only when he asked it to talk."),
    Capability("leave_meeting", "teams", http="POST /api/meetings/leave", write=True,
               note="Hangs up. Safe to call when not in a call — it says so."),
    Capability("meeting_notes", "teams", http="GET /api/meetings/notes",
               note="Live captions Asta captured while in a call — the answer to 'what "
                    "did I miss'. Speech recognition, and only the part Asta attended: "
                    "summarise what is there, never fill in what is not."),
    Capability("say_in_call", "teams", http='POST /api/meetings/say {"text":"…"}',
               write=True,
               note="ONLY the words Arun gave you, never improvised and never an answer on "
                    "his behalf. Usually unavailable (needs a virtual mic); when it is, it "
                    "tells you nothing was said rather than pretending."),
    Capability("outlook_mail", "outlook",
               shell="python -m app.outlook mail [limit]  ·  python -m app.outlook attention"),
    Capability("outlook_meetings", "outlook", shell="python -m app.outlook meetings"),
    Capability("meeting_prep", "outlook", http="GET /api/meeting-prep?title={title}",
               note="Drafts prep for a meeting/1:1 (talking points, questions, watch-outs). "
                    "Draft only — stage with prepare_to_send to actually send it to anyone."),
    Capability("create_meeting", "outlook",
               http='POST /api/meetings {"subject":"…","when":"YYYY-MM-DD HH:MM",'
                    '"minutes":30,"attendees":"a@b.com,c@d.com","agenda":"…"}',
               write=True,
               note="STAGES the invite; his yes sends it. Resolve 'Thursday at 3' to a real "
                    "date YOURSELF and ask if unsure — a wrong day books other people's time."),
    Capability("request_leave", "outlook",
               http='POST /api/leave {"start_date":"YYYY-MM-DD","end_date":"",'
                    '"reason":"…","to":"manager@company.com"}',
               write=True,
               note="STAGES an all-day leave invite. Both dates INCLUSIVE — one day off is "
                    "the same date twice or no end date. Goes to whoever approves it, so it "
                    "never sends on your judgement."),
    Capability("meeting_recap", "outlook",
               http='POST /api/meeting-recap {"transcript":"…","title":"…"}',
               note="Summarizes a call/meeting transcript (from Teams' own recording/recap) "
                    "into decisions + action items + open questions; pings Arun if an item is his."),
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
               note="Produces notes for ARUN. Read-only — to actually post them, use "
                    "pr_review_post, and only when he asked you to."),
    Capability("merge_pr", "tasks",
               http='POST /api/pr-merge {"pr":"123","workspace":"…","repo":"","method":"squash"}',
               write=True,
               note="STAGES a merge; his yes performs it. The least reversible thing here — "
                    "it puts code on the branch everyone builds from — so it refuses "
                    "outright on red CI, unfinished CI, conflicts, a draft, or requested "
                    "changes, and says which. Never work around a blocker; tell him what "
                    "is in the way. Only when he asks to merge in those words."),
    Capability("debug_stack_health", "workspace", http="POST /api/debug-stack",
               note="Read-only. Reach for it when a Grafana/Temporal/Jira answer comes "
                    "back EMPTY — an empty answer from a broken tool and an empty answer "
                    "from a healthy system look identical, and only this tells them "
                    "apart. Never report 'nothing found' on an env whose cert is broken."),
    Capability("check_teams_selectors", "teams", http="POST /api/teams/selector-check",
               note="Read-only DOM check. Runs daily by itself — call it when Teams "
                    "reads have gone suspiciously quiet, or right after Microsoft "
                    "ships a Teams update. It never guesses a replacement selector: "
                    "one chosen blind is how a message lands in the wrong thread."),
    Capability("answer_quality", "workspace", http="POST /api/evals {\"workspace\":\"booking\"}",
               note="Spends a brain call per case, so not routine. Say the score AND "
                    "which cases failed — a bare percentage tells him nothing about "
                    "what to fix."),
    Capability("pr_review_post", "tasks",
               http='POST /api/pr-review {"pr":"123","action":"approve|comment|'
                    'request_changes","body":"…","workspace":"…","repo":""}',
               write=True,
               note="STAGES a review under Arun's name; his yes posts it verbatim. Only "
                    "when he explicitly says to post/approve — an approval is visible to "
                    "the whole team and cannot be taken back quietly."),
    Capability("list_background_tasks", "tasks", http="GET /api/tasks"),
    Capability("task_result", "tasks", http="GET /api/tasks/{id}"),
    Capability("approve_task", "tasks", http="POST /api/tasks/{id}/approve", write=True,
               note="At a plan gate this means implement. Any other feedback goes to "
                    'POST /api/tasks/{id}/reply {"text":"…"} and the pipeline re-plans.'),
    Capability("ship_task", "tasks", http="POST /api/tasks/{id}/ship", write=True,
               note="Pushes the branch and opens the PR. The pipeline NEVER does this "
                    "itself — only when Arun has seen the diff and said ship. The task "
                    "stays OPEN afterwards, tracked until the PR merges or closes."),
    Capability("refine_task", "tasks", http='POST /api/tasks/{id}/refine {"text":"…"}',
               write=True,
               note="THE tool for any comment on work a task already delivered — a "
                    "correction, 'also handle X', a review comment, a red PR build. "
                    "NEVER spawn a new task for feedback: refine continues the original "
                    "task in its own session, so it keeps everything it already worked "
                    "out. A new task would re-derive it all and reimplement the change."),
    Capability("task_pr_status", "tasks", http="GET /api/tasks/prs",
               note="Where shipped work stands — CI, review, merged or not. Use it for "
                    "'what's pending', 'did that merge', 'any PR blocked'."),
    Capability("reject_task", "tasks", http="POST /api/tasks/{id}/reject", write=True,
               note="Throws the work away — the branch and its diff go with it. Only when "
                    "Arun says to drop it; if he is merely unhappy with the result, reply "
                    "with the feedback instead so the pipeline re-plans."),
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
    Capability("watch_ci", "ops", http='POST /api/ci/watch {"what":"release","repo":""}',
               note="The watcher is quiet by design — only runs Arun triggered and PRs he "
                    "authored. Use this when he names a build he wants followed anyway."),
    Capability("trace_report", "ops", http="GET /api/traces"),
    Capability("token_audit", "ops", http="GET /api/token-audit?hours={hours}"),
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


#: Set for the whole life of a turn that runs ALONGSIDE another one.
#:
#: A read-only question — "what's the CI status", "what did Vinish say" — has no
#: conflict with work already running, and queueing it behind a forty-minute
#: implementation is why Arun got "still finishing the previous one" instead of an
#: answer. So those are answered concurrently.
#:
#: The safety comes from here rather than from the classifier being right. A
#: side turn simply cannot reach a capability that writes, so the worst a
#: misclassified message can do is read something and answer. That is the
#: difference between a heuristic that has to be perfect and one that only has to
#: be useful.
#:
#: A ContextVar because asyncio copies the context when a task is created: set it
#: before spawning the side turn and it applies to that turn and everything it
#: awaits, with no flag threaded through five signatures — and the parent turn,
#: created earlier, is unaffected.
READ_ONLY_TURN: ContextVar[bool] = ContextVar("asta_read_only_turn", default=False)

#: What Arun actually typed this turn, so a tool can check whether it is doing the
#: thing he asked for or something else entirely.
#:
#: A tool only ever sees the arguments the model chose, which is precisely the
#: wrong vantage point for noticing a substitution: `delegate_task("fix the ETA
#: validation", ...)` looks identical whether he asked for that or asked for a
#: phone call. The model is not a reliable narrator of its own instructions —
#: it is the thing being checked — so the original words have to arrive by a
#: route it does not control. Same ContextVar mechanism as READ_ONLY_TURN: set
#: once at the turn boundary, copied into everything the turn awaits.
TURN_TEXT: ContextVar[str] = ContextVar("asta_turn_text", default="")


def chat_may_write() -> bool:
    """May a CHAT turn edit files, commit, push or open a PR? Default: no.

    One answer for every brain, because the alternative was tried and failed:
    Copilot carried `--deny-tool edit` and Claude carried nothing at all, so the
    same message reached two different sets of rules depending on which brain
    happened to be selected. That is the shape of bug that costs an evening.

    Code work belongs in the task lane, where it plans and STOPS for his
    approval. A chat turn is capped at ASTA_TURN_TIMEOUT, cannot ask for that
    approval, and has none of the branch discipline — so an implementation
    started here ends as a half-finished edit and a timeout, which is exactly
    what it did.

    Honest about its reach: this closes the named acts, not every conceivable
    one. A brain with a shell can still write a file through `sed -i` or a
    python one-liner, and no deny list fixes that. The real guarantee is the
    ROUTING — work_intent sends code work to the lane before a brain is asked —
    and this is the second lock on the same door, not the door.

    `ASTA_CHAT_MAY_EDIT=1` puts chat editing back for anyone who wants it.
    """
    return os.environ.get("ASTA_CHAT_MAY_EDIT", "0").strip().lower() in ("1", "true", "yes", "on")


def writes(name: str) -> bool:
    cap = registry().get(name)
    return bool(cap and cap.write)


def tools_for(selected: list[str] | tuple[str, ...] | None = None) -> list[Callable]:
    """The callables to hand pydantic-ai. None = everything (the old behaviour)."""
    reg = registry()
    read_only = READ_ONLY_TURN.get()
    if selected is None:
        return [c.fn for c in reg.values() if not (read_only and c.write)]
    keep = list(dict.fromkeys(list(selected) + [n for n in ALWAYS if n in reg]))
    return [reg[n].fn for n in keep if n in reg and not (read_only and reg[n].write)]


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


def cli_block(port: str = "8321", root: str = "",
              selected: list[str] | tuple[str, ...] | None = None) -> str:
    """How a CLI brain reaches these same capabilities.

    Generated from the table, so a new tool is taught to Copilot and Claude the
    moment it is added to chat — no second description to update, and no stale
    copilot session to clear.

    `selected` narrows the FULL spec (name + endpoint + summary) to the tools
    this turn is likely to need — the same per-turn ranking the in-process agent
    gets, which for CLI brains was previously thrown away, so every turn paid for
    all ~34. It is a hint, not a fence: everything omitted is still listed by
    name and one line at the bottom, so a mis-rank costs a little indirection
    (the brain reads the index, then asks), never a capability it cannot reach.
    None means "no ranking" — the old full block.
    """
    reg = registry()
    if selected is None:
        chosen = set(reg)
    else:
        chosen = set(selected) & set(reg)
        chosen |= set(ALWAYS)                          # the floor is never dropped
        chosen |= _complete_groups(chosen)             # half a group is a trap
    http = [c for c in reg.values() if c.http and c.name in chosen]
    shell = [c for c in reg.values() if c.shell and c.name in chosen]
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
    # Anything not expanded above is still discoverable by name + one line, so
    # nothing is truly hidden — the brain can ask for the endpoint of a listed
    # tool. Endpoint-less tools (in-process only, e.g. memory) are omitted: a CLI
    # brain cannot curl them, so listing one to "ask for its endpoint" would send
    # it chasing something that does not exist.
    rest = [c for c in reg.values()
            if c.name not in chosen and (c.http or c.shell)]
    if rest:
        rows = "\n".join(f"  {c.name} — {c.summary}" for c in rest)
        parts.append(
            "Also available (ask for the exact endpoint/command when you need one):\n" + rows)
    # Rules travel with the tool they constrain: showing a rule for a tool that
    # was not expanded is noise, so the RULES block narrows with the selection.
    rules = [f"- {c.name}: {c.note}" for c in reg.values()
             if c.note and c.name in chosen]
    if rules:
        parts.append("RULES (these are the expensive ones to get wrong):\n" + "\n".join(rules))
    return "\n\n".join(parts)


def _complete_groups(names: set[str]) -> set[str]:
    """Every capability sharing a group with a selected one. A brain that can
    read a Jira issue but not comment on it improvises something worse than
    asking, so groups travel whole — the same rule the in-process selector uses."""
    reg = registry()
    groups = {reg[n].group for n in names if n in reg}
    return {n for n, c in reg.items() if c.group in groups}


def index_block(selected: list[str] | tuple[str, ...] | None = None) -> str:
    """One line per capability — for a brain that has the names but not the schemas."""
    reg = registry()
    chosen = reg.values() if selected is None else [reg[n] for n in selected if n in reg]
    return "\n".join(f"- **{c.name}** ({c.group}) — {c.summary}" for c in chosen)
