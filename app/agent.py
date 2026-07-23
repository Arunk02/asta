"""The Asta agent: one PydanticAI agent, swappable models, memory + workspace tools."""

from __future__ import annotations

import os

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from . import memory, skills, untrusted, workspace_tools

def assistant_name() -> str:
    return os.environ.get("ASSISTANT_NAME", "Asta")


PERSONA = """You are {name}, Arun's personal engineering assistant, running locally on his laptop.

Style: conversational, sharp, proactive — a capable colleague, not a search engine. Keep answers
tight; expand only when asked or when correctness demands it.

Agency: act, don't just answer. When a request needs several steps, chain the tools yourself
end-to-end (resolve context -> read files -> check logs -> conclude) without asking permission
between steps. When Arun assigns work ("fix X", "implement Y", a Jira key), delegate it as a
background task right away and tell him the task id. After answering, if there's an obvious next
move (work to delegate, a fact to verify in grafana/temporal, a follow-up he'll likely want),
offer it in one short line. Ask a clarifying question only when the answer genuinely changes
what you'd do — and when it does, ask_user rather than stopping the work.

Role: you are the ORCHESTRATOR. Arun's office pays for Copilot, so hands-on implementation work
is delegated to the CLI executors (via background tasks) rather than burned on API tokens; you
plan, coordinate, verify, and keep the memory. When Claude tokens run out, routine chat is
automatically re-routed to Copilot CLI — never refuse work because of that.

Memory: use the remember tool whenever Arun corrects you, states a preference, or a debugging
session uncovers a root cause / fix / environment gotcha worth keeping. Do it silently as part
of answering; mention it in one short parenthetical at most.

MCP tools (temporal, grafana, github, docs) give you live debugging of workflows, logs and
metrics. HARD RULE for grafana_* tools: call load_skill('grafana-analyser') first (once per
conversation) and follow its query discipline exactly — namespace-wide Loki first, one wide-window
call for identifier traces, aggregates before raw lines, limit <= 50, no per-service loops,
Prometheus/Tempo only for performance asks. The atlassian_* MCP tools are the optional fallback
for what the Jira REST tools don't cover (Confluence, creating issues) — never for plain reads.

CODE WORK — the flow Arun expects, with a message to him at EVERY step:
plan → HIS approval → implement → "code done" → he says raise the PR → ship (commit, push, PR)
→ "PR raised" → CI watched → "CI passed/failed" → then ASK whether to post it in a group/DM for
review, and only post if he says so, and only where he names. Never plan or implement in chat
yourself, never commit or open a PR unprompted, and never mention Claude/Copilot/AI in a commit
message or PR body — Arun's commits must read as his own work.

Meeting recaps and summaries: ONLY when Arun pastes or asks — never proactively.

If a tool fails, say what failed and continue with what you have.
"""


CHANNEL_NOTES = {
    "whatsapp": "Channel: WhatsApp. Reply in plain text (no markdown), max ~120 words. "
                "For long content give the headline and say the full detail is in the web UI.",
    "voice": "Channel: voice. Reply conversationally in short sentences that sound natural read aloud.",
}


def build_instructions(conversation_summary: str, recall_block: str, workspace: str | None,
                       channel: str = "web",
                       selected: list[str] | tuple[str, ...] | None = None) -> str:
    """The system prompt for one turn.

    The persona holds identity, agency and the rules that are not about any one
    tool. Per-tool rules ride with the capability registry instead, so when the
    toolset narrows for a message the rules narrow with it — and a tool can
    never be exposed with its hard rule left behind.
    """
    from . import capabilities
    # The safety policy rides in the instructions, ahead of anything external,
    # so it is in context before the first wrapped block arrives.
    parts = [PERSONA.format(name=assistant_name()), untrusted.POLICY]
    notes = capabilities.notes_block(selected)
    if notes:
        parts.append(notes)
    if channel in CHANNEL_NOTES:
        parts.append(CHANNEL_NOTES[channel])
    idx = memory.index_text()
    if idx.strip():
        parts.append("## Memory index\n" + idx)
    skills_idx = skills.index_block()
    if skills_idx:
        parts.append(skills_idx)
    if recall_block:
        parts.append("## " + recall_block)
    if conversation_summary:
        parts.append("## Earlier in this conversation (compacted)\n" + conversation_summary)
    if workspace:
        parts.append(f"Active workspace: **{workspace}** — default to it for workspace tools.")
    return "\n\n".join(parts)


# --- model registry ----------------------------------------------------------

def _lmstudio_model_id() -> str | None:
    base = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").rstrip("/")
    try:
        data = httpx.get(f"{base}/models", timeout=2).json().get("data", [])
        return data[0]["id"] if data else None
    except Exception:
        return None


_INTENT_SYSTEM = (
    "The assistant is in the MIDDLE of a task. Classify the user's new message "
    "by how it relates to that in-progress task. Answer with ONE word only:\n"
    "augment - adds to or refines it; keep working\n"
    "redirect - changes or cancels it; the current work should stop\n"
    "status - only asking for progress\n"
)


def _intent_word(raw: str | None, last_line: bool = False) -> str | None:
    text = (raw or "").strip()
    if last_line:
        # A CLI brain may echo the prompt (which names all three words), so trust
        # only its final line.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        text = lines[-1] if lines else ""
    word = text.lower()
    for k in ("redirect", "augment", "status"):
        if k in word:
            return k
    return None


# Same phrasing shouldn't be re-billed. Small and bounded; interjections repeat a
# lot ("also add tests", "no wrong file").
_INTENT_CACHE: dict[tuple[str, str], str] = {}
_INTENT_CACHE_MAX = 256


async def quick_intent(text: str, model_name: str = "") -> str | None:
    """Classify a mid-turn interjection: 'augment' | 'redirect' | 'status', or None.

    Runs on whichever brain the conversation is set to — the model picker in the UI
    is the single source of truth, and that applies here too. There's no hidden
    cascade: pick copilot and it classifies on copilot, pick local and it's free.
    Only reached for phrasings the (free) heuristics couldn't settle, and the answer
    is cached, so a paid brain is asked rarely. None means "couldn't decide" and the
    caller keeps its safe default rather than guessing.

    ASTA_INTENT_BRAIN pins a specific brain regardless of the picker, or 'off'
    to stay heuristics-only.
    """
    name = (os.environ.get("ASTA_INTENT_BRAIN", "").strip().lower()
            or model_name or default_chat_model())
    if name in ("off", "none", "test"):
        return None
    key = (name, " ".join((text or "").lower().split())[:200])
    if key in _INTENT_CACHE:
        return _INTENT_CACHE[key]
    try:
        if is_cli(name):
            # Any CLI model classifies the same way: one word, lowest effort,
            # tight timeout — and the cache above keeps it from repeating.
            verdict = await _intent_cli(name, text)
        elif name == "local":
            verdict = await _intent_local(text)
        elif name == "claude":
            verdict = await _intent_anthropic(text)
        elif name == "openai":
            verdict = await _intent_openai(text)
        else:
            verdict = None
    except Exception:
        return None
    if verdict:
        if len(_INTENT_CACHE) >= _INTENT_CACHE_MAX:
            _INTENT_CACHE.clear()
        _INTENT_CACHE[key] = verdict
    return verdict


async def _intent_cli(name: str, text: str) -> str | None:
    """Any CLI model (Copilot, Claude Code, …). These are real agentic sessions,
    so we keep it to one word at the lowest effort and lean on the cache.

    last_line=True because a CLI may echo the prompt — which names all three
    verdict words — and only its final line is the actual answer.
    """
    mod = runner(name)
    if not mod.available():
        return None
    prompt = f"{_INTENT_SYSTEM}\nMessage: {(text or '')[:400]}\nAnswer with one word:"
    out = await mod.one_shot(prompt, timeout=25, effort="low")
    return _intent_word(out, last_line=True)


async def _intent_anthropic(text: str) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.environ.get("ASTA_INTENT_MODEL", "claude-haiku-4-5-20251001")
    async with httpx.AsyncClient(timeout=6) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 3, "temperature": 0,
                  "system": _INTENT_SYSTEM,
                  "messages": [{"role": "user", "content": (text or "")[:400]}]},
        )
    blocks = r.json().get("content") or []
    return _intent_word(blocks[0].get("text") if blocks else None)


async def _intent_openai(text: str) -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    model = os.environ.get("ASTA_INTENT_OPENAI_MODEL", "gpt-4o-mini")
    async with httpx.AsyncClient(timeout=6) as c:
        r = await c.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "temperature": 0, "max_tokens": 3,
                  "messages": [{"role": "system", "content": _INTENT_SYSTEM},
                               {"role": "user", "content": (text or "")[:400]}]},
        )
    return _intent_word(r.json()["choices"][0]["message"]["content"])


async def _intent_local(text: str) -> str | None:
    mid = _lmstudio_model_id()
    if not mid:
        return None
    base = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").rstrip("/")
    async with httpx.AsyncClient(timeout=4) as c:
        r = await c.post(f"{base}/chat/completions", json={
            "model": mid, "temperature": 0, "max_tokens": 3,
            "messages": [
                {"role": "system", "content": _INTENT_SYSTEM},
                {"role": "user", "content": (text or "")[:400]},
            ],
        })
    return _intent_word(r.json()["choices"][0]["message"]["content"])


# --- model specs -------------------------------------------------------------
# One table every branch reads, so "which models are supported" is answered in
# exactly one place. Adding a model = one entry here, not a hunt through
# agent/main/tasks for hardcoded name checks.
#
#   kind      "cli" = an agentic CLI we drive as a subprocess (has its own tools,
#             can edit files, run builds); "api" = a pydantic-ai chat model.
#   runner    module name under app/ exposing available()/run_turn()/one_shot().
#   executes  can drive a background task pipeline. CLI-only by nature: the
#             project context pipeline needs a tool-using agent that edits the repo and
#             runs builds, which a plain chat completion cannot do.
#   effort    honours the low|medium|high|xhigh|max ladder.
_SPECS: dict[str, dict] = {
    # Office-paid workhorse, default for day-to-day chat.
    "copilot":    {"kind": "cli", "runner": "copilot_cli", "executes": True,  "effort": True,
                   "exec_name": "copilot",
                   "label": "Copilot CLI (office)", "hint": "install/auth: copilot login"},
    # Runs on the Claude subscription Arun already pays for — listed above the
    # API-key "claude" entry, which bills a second prepaid account for the same model.
    # exec_name: background tasks have called this executor "claude" since before
    # it was a chat option, and that string is persisted in kv rows — so the
    # table carries both names rather than migrating live data.
    "claude_cli": {"kind": "cli", "runner": "claude_cli", "executes": True,  "effort": True,
                   "exec_name": "claude",
                   "label": "Claude CLI (subscription)", "hint": "install/auth: claude login"},
    "claude":     {"kind": "api", "env": "ANTHROPIC_API_KEY", "executes": False, "effort": False,
                   "model_env": ("ASTA_CLAUDE_MODEL", "claude-sonnet-5"), "label": "Claude"},
    "openai":     {"kind": "api", "env": "OPENAI_API_KEY", "executes": False, "effort": True,
                   "model_env": ("ASTA_OPENAI_MODEL", "gpt-4o"), "label": "OpenAI"},
    "local":      {"kind": "api", "env": "", "executes": False, "effort": False,
                   "model_env": ("", ""), "label": "Local"},
}

# Task executors are the CLI models — kept as derived lists so they can never
# drift from the table above.
EXECUTORS = tuple(n for n, s in _SPECS.items() if s["executes"])
EXECUTOR_NAMES = tuple(s["exec_name"] for s in _SPECS.values() if s["executes"])
_BY_EXEC_NAME = {s["exec_name"]: n for n, s in _SPECS.items() if s["executes"]}


def from_exec_name(name: str) -> str:
    """Executor string -> canonical spec key.

    Must be separate from normalize_model: "claude" is BOTH the executor string
    for the Claude CLI and the picker key for the Anthropic API model. Which one
    is meant depends on who is asking, so the caller picks the right lookup —
    resolving it by string alone silently sent task effort to the API spec
    (which has no effort ladder) and every stage came back empty.
    """
    return _BY_EXEC_NAME.get((name or "").strip(), (name or "").strip())


def normalize_model(name: str) -> str:
    """Picker name -> canonical spec key (spec keys win; see from_exec_name)."""
    name = (name or "").strip()
    return name if name in _SPECS else _BY_EXEC_NAME.get(name, name)


def exec_name(name: str) -> str:
    """Canonical spec key -> the string tasks persist for that executor."""
    return spec(normalize_model(name)).get("exec_name", name)


def spec(name: str) -> dict:
    return _SPECS.get(name, {})


def is_cli(name: str) -> bool:
    return spec(name).get("kind") == "cli"


def runner(name: str):
    """The module that drives a CLI model (copilot_cli / claude_cli)."""
    mod = spec(name).get("runner")
    if not mod:
        raise ValueError(f"{name} is not a CLI-backed model")
    from importlib import import_module
    return import_module(f".{mod}", __package__)


def available(name: str) -> bool:
    s = spec(name)
    if not s:
        return False
    if s["kind"] == "cli":
        return runner(name).available()
    if name == "local":
        return bool(_lmstudio_model_id())
    return bool(os.environ.get(s["env"], ""))


# Stage effort, per model. The cascade lets one dial cover everything and a
# specific model override it: any model can be picked at any time, so none of
# them should need bespoke wiring.
#   ASTA_EFFORT_<MODEL>_<STAGE>  most specific
#   ASTA_EFFORT_<STAGE>          all models
#   COPILOT_EFFORT_<STAGE>         legacy name, still honoured
_STAGE_DEFAULT = {"ANALYSIS": "low", "PLAN": "medium", "CODE": "high", "TASK": "medium"}


def effort_for(model: str, stage: str) -> str:
    """Reasoning effort for a stage on a given model ('' when it has no ladder).

    Accepts a picker name or an executor name — callers on both sides of the
    system pass whichever they hold.
    """
    stage = stage.upper()
    model = normalize_model(model)
    if not spec(model).get("effort", False):
        return ""
    key = model.upper()
    for var in (f"ASTA_EFFORT_{key}_{stage}", f"ASTA_EFFORT_{stage}",
                f"COPILOT_EFFORT_{stage}"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return _STAGE_DEFAULT.get(stage, "")


def model_registry() -> dict[str, dict]:
    """name -> {label, available, detail}. Availability drives the UI picker."""
    local_id = _lmstudio_model_id()
    registry: dict[str, dict] = {}
    for name, s in _SPECS.items():
        ok = available(name)
        if s["kind"] == "cli":
            label, hint = s["label"], s["hint"]
        elif name == "local":
            label = f"Local ({local_id})" if local_id else "Local (LM Studio)"
            hint = "start LM Studio and load a model"
        else:
            env_var, default = s["model_env"]
            label = f"{s['label']} ({os.environ.get(env_var, default)})"
            hint = f"set {s['env']} in .env"
        registry[name] = {"label": label, "available": ok, "detail": "" if ok else hint}
    if os.environ.get("ASTA_TEST_MODEL"):
        registry["test"] = {"label": "Test (no LLM)", "available": True, "detail": ""}
    return registry


def best_model_name() -> str:
    """Best available API-backed model for background jobs (mission planning, digests).

    Copilot is deliberately excluded here — it is CLI-backed, not a pydantic-ai Model;
    callers fall back to copilot_cli.one_shot() when this raises.
    """
    reg = model_registry()
    for name in ("claude", "openai", "local", "test"):
        if reg.get(name, {}).get("available"):
            return name
    raise RuntimeError("No API model available — add a key to .env or start LM Studio")


def default_chat_model() -> str:
    """Day-to-day default: Copilot CLI (office-paid) when present, else best API model."""
    reg = model_registry()
    if reg.get("copilot", {}).get("available"):
        return "copilot"
    return best_model_name()


def get_model(name: str):
    if name == "test" and os.environ.get("ASTA_TEST_MODEL"):
        from pydantic_ai.models.test import TestModel
        return TestModel(
            call_tools=[],
            custom_output_text="Test model online. The chat pipeline (WS → agent → stream → store) works.",
        )
    if name == "claude":
        return AnthropicModel(os.environ.get("ASTA_CLAUDE_MODEL", "claude-sonnet-5"))
    if name == "openai":
        return OpenAIChatModel(os.environ.get("ASTA_OPENAI_MODEL", "gpt-4o"))
    if name in ("copilot", "claude_cli"):
        # CLI-backed, not pydantic-ai Models — the chat loop routes these via
        # copilot_cli.run_turn / claude_cli.run_turn.
        raise RuntimeError(f"{name} is CLI-backed; handled by its own module, not get_model()")
    if name == "local":
        base = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
        local_id = _lmstudio_model_id()
        if not local_id:
            raise RuntimeError("LM Studio is not running (no model at " + base + ")")
        return OpenAIChatModel(local_id, provider=OpenAIProvider(base_url=base, api_key="lm-studio"))
    raise ValueError(f"Unknown model '{name}'")


def model_settings(name: str):
    if name == "claude":
        # Cache the stable prefix (instructions + tool defs) -> ~90% cheaper repeat turns.
        return AnthropicModelSettings(
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
        )
    return None


# --- agent tools -------------------------------------------------------------

def remember(title: str, fact: str, kind: str = "fact") -> str:
    """Store a durable memory. kind: fact | preference | gotcha | fix.
    Use for corrections, preferences, root causes, environment quirks."""
    path = memory.remember(title, fact, kind)
    return f"Remembered in {path}"


def load_skill(name: str) -> str:
    """Load a skill playbook by name (see the skills list in your instructions). Call once
    per conversation before working in that skill's area, then follow it strictly."""
    from . import learn
    body = skills.load(name)
    if body:
        # Being loaded is the only evidence a skill earns its place; pruning
        # reads these counters.
        learn.record_use(name)
    if body is None:
        available = ", ".join(s["name"] for s in skills.discover()) or "(none)"
        return f"No skill '{name}'. Available: {available}"
    return body


def search_memory(query: str) -> str:
    """Search long-term memory for facts/episodes beyond what was auto-recalled."""
    hits = memory.recall(query, k=6)
    if not hits:
        return "No matching memories."
    return "\n".join(f"[{h['mtype']}] {h['title']} ({h['path']}): {h['snippet']}" for h in hits)


async def resolve_context(workspace: str, task: str) -> str:
    """project context librarian: given a task/question, returns the exact services, files and
    line numbers to read in the named workspace. Call this FIRST
    for any code question."""
    return untrusted.wrap(await workspace_tools.resolve_context(workspace, task),
                          f"workspace resolver: {workspace}")


def read_workspace_file(workspace: str, rel_path: str, start_line: int = 1, end_line: int = 0) -> str:
    """Read a file (or line range) from a registered workspace.
    rel_path is relative to the workspace root."""
    return untrusted.wrap(
        workspace_tools.read_workspace_file(workspace, rel_path, start_line, end_line),
        f"{workspace}/{rel_path}")


def list_services(workspace: str) -> str:
    """List the service repos inside a registered workspace."""
    return "\n".join(workspace_tools.list_services(workspace))


async def jira_search(jql: str) -> str:
    """Search Jira with a JQL query. Returns key, summary, status, assignee per issue."""
    from . import jira
    issues = await jira.search(jql)
    if not issues:
        return "No issues match."
    return "\n".join(f"{i['key']} [{i['status']}] {i['summary']} — {i['assignee'] or 'unassigned'}" for i in issues)


async def jira_my_issues() -> str:
    """List Arun's open Jira issues."""
    return await jira_search("assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC")


async def jira_issue(key: str) -> str:
    """Full detail of one Jira issue (summary, status, description)."""
    from . import jira
    i = await jira.get_issue(key)
    head = (f"{i['key']} [{i['type']} / {i['status']} / {i['priority']}] {i['summary']}\n"
            f"Labels: {', '.join(i['labels']) or '-'} Components: {', '.join(i['components']) or '-'}")
    # Anyone with tracker access can write the summary, description and comments.
    return head + "\n\n" + untrusted.wrap(
        i["description"] or "(no description)", f"Jira {i['key']}")


async def jira_comment(key: str, text: str) -> str:
    """Post a comment on a Jira issue. Confirm the wording with Arun first unless he
    dictated the exact text."""
    from . import jira
    r = await jira.add_comment(key, text)
    return f"Comment posted on {r['key']} (id {r['comment_id']})."


async def jira_transition(key: str, to_status: str) -> str:
    """Move a Jira issue to another status by name (e.g. 'In Progress', 'Ready for Retest').
    Confirm with Arun first unless he stated the exact target status. If the status isn't
    reachable, the error lists the valid targets — offer those to Arun."""
    from . import jira
    try:
        r = await jira.transition_issue(key, to_status)
    except RuntimeError as e:
        return str(e)
    return f"{r['key']} moved to {r['status']}."


async def refresh_context(workspace: str) -> str:
    """Re-check context drift and regenerate a workspace's project context graph."""
    from . import refresh
    return await refresh.refresh_workspace(workspace, reason="requested in chat")


async def review_pr(pr: str, workspace: str, repo: str = "") -> str:
    """Review a pull request and produce reviewer notes for Arun to post.

    pr: a number ("123"), a URL, or a branch. repo: the service directory, needed when the
    workspace holds several repos. Gathers the PR, its diff, its CI checks and the project
    context, then runs the review as a background task — reviews are slow, so the chat
    stays free and Arun is notified when the notes are ready. Read-only: it never comments
    on the PR or approves it. Use for 'review PR 123', 'what do you think of this PR'."""
    from . import review, tasks
    try:
        text, meta = await review.brief(pr, workspace, repo)
    except (RuntimeError, ValueError) as exc:
        return f"Could not read that PR: {exc}"
    t = tasks.spawn(f"Review PR #{meta['number']}: {meta['title'][:60]}", text,
                    "analysis", workspace or None)
    return (f"Task #{t['id']} — reviewing PR #{meta['number']} "
            f"({meta.get('changedFiles', 0)} files, +{meta.get('additions', 0)}/"
            f"-{meta.get('deletions', 0)}). Arun gets the notes when it finishes.")


def quality_report(days: int = 7) -> str:
    """How well the work has actually been landing: plans approved as-is vs re-planned,
    tasks finished vs failed, drafts sent unedited, questions answered, PRs opened, skills
    learned. Use when Arun asks how Asta is doing, or before suggesting a change to how
    work is run — this is the evidence, token_audit is only the cost."""
    from . import quality
    return quality.report(days)


def trace_report(limit: int = 15) -> str:
    """Your own performance/token telemetry: per-model latency + token totals (7d) and the
    last N turns (model, ms, tokens in/out/cached, prompt sizes, tools, errors). Use when
    Arun asks why something was slow/expensive, or to find token waste."""
    from . import store
    lines = ["PER-MODEL (last 7 days):"]
    for s in store.trace_summary():
        lines.append(
            f"  {s['model']}: {s['turns']} turns, avg {s['avg_ms']}ms (max {s['max_ms']}ms), "
            f"tokens in/out/cached {s['input']}/{s['output']}/{s['cached']}, "
            f"avg prompt {s['avg_instr_chars']} chars, errors {s['errors']}")
    lines.append(f"\nLAST {limit} TURNS (newest first):")
    for t in store.list_traces(limit):
        ft = f" first {t['first_token_ms']}ms," if t["first_token_ms"] else ""
        err = f" ERROR: {t['error'][:80]}" if t["error"] else ""
        tools = f" tools={','.join(t['tools'])}" if t["tools"] else ""
        lines.append(
            f"  [{t['model']}/{t['channel']}] {t['total_ms']}ms,{ft} "
            f"tok {t['input_tokens']}/{t['output_tokens']}/{t['cached_tokens']}, "
            f"instr {t['instructions_chars']}ch prompt {t['prompt_chars']}ch{tools}{err}")
    return "\n".join(lines)


def set_reminder(text: str, due_iso: str, repeat: str = "") -> str:
    """Set a reminder that fires on WhatsApp/Telegram/UI. due_iso: LOCAL time ISO format
    e.g. '2026-07-19T15:00'. repeat: '' (once) | daily | weekdays | weekly."""
    from . import reminders
    try:
        r = reminders.create(text, due_iso, repeat)
    except ValueError as exc:
        return f"Couldn't set reminder: {exc}"
    import datetime as dt
    when = dt.datetime.fromtimestamp(r["due_at"]).strftime("%a %d %b %H:%M")
    return f"Reminder #{r['id']} set for {when}" + (f" (repeats {repeat})" if repeat else "")


def list_my_reminders() -> str:
    """List pending reminders."""
    import datetime as dt
    from . import store
    rows = store.list_reminders()
    if not rows:
        return "No pending reminders."
    return "\n".join(
        f"#{r['id']} {dt.datetime.fromtimestamp(r['due_at']).strftime('%a %d %b %H:%M')} — "
        f"{r['text']}" + (f" (repeats {r['repeat']})" if r["repeat"] else "")
        for r in rows)


def cancel_reminder(reminder_id: int) -> str:
    """Cancel a pending reminder by id."""
    from . import reminders
    try:
        reminders.cancel(reminder_id)
        return f"Reminder #{reminder_id} cancelled."
    except ValueError as exc:
        return str(exc)


async def morning_brief() -> str:
    """Generate the morning brief now (finished work, approvals waiting, Jira movement,
    today's reminders, health). Also runs automatically on weekday mornings."""
    from . import briefing
    return await briefing.morning_brief()


async def standup_draft() -> str:
    """Draft Arun's standup from yesterday's real git commits, finished missions/tasks
    and Jira movement. Also runs automatically on weekday mornings."""
    from . import briefing
    return await briefing.standup_draft()


async def health_check() -> str:
    """Probe all channels/integrations (WhatsApp, Telegram, Teams session, Copilot,
    LM Studio, disk) and report what's broken."""
    from . import health
    problems = await health.run_check(notify_transitions=False)
    return health.report_text(problems)


async def ci_status() -> str:
    """Recent GitHub Actions runs across all workspace repos (needs `gh auth login` once).
    Failures are also pushed to Arun automatically every 10 min."""
    from . import ci_watch
    return await ci_watch.recent_runs()


async def ask_user(question: str) -> str:
    """Ask Arun ONE short question and wait for his answer, without stopping anything.

    Use it when the answer genuinely changes what you'd do and you cannot infer it —
    which of two repos he means, which of two people, whether a number is a booking id
    or a plan number. The question goes to his phone and this call returns his reply.
    Do NOT use it for permission to act (that is what the approval gates are for), and
    do not use it when a sensible assumption you can state would do."""
    from . import asking
    return await asking.ask(question, source="chat")


def delegate_task(title: str, prompt: str, kind: str = "analysis",
                  workspace: str = "", teams_chat: str = "") -> str:
    """Spawn a background worker so the chat stays free; Arun gets a WhatsApp/Telegram
    notification with the result. The prompt must be SELF-CONTAINED (the worker has no
    chat context). kind: analysis (read-only, parallel) | code (edits code — set
    the workspace) | teams_draft (drafts a Teams reply — set teams_chat; the
    draft waits for Arun's approval, it is never sent automatically)."""
    from . import tasks
    t = tasks.spawn(title, prompt, kind, workspace or None, teams_chat)
    return (f"Task #{t['id']} ({kind}) spawned — running in the background. "
            f"Arun will be notified when it finishes.")


def list_background_tasks() -> str:
    """List recent background tasks with status."""
    from . import store
    rows = store.list_tasks()
    if not rows:
        return "No background tasks yet."
    return "\n".join(
        f"#{t['id']} [{t['status']}] ({t['kind']}) {t['title']}"
        + (f" → {t['teams_chat']}" if t["teams_chat"] else "")
        for t in rows)


def task_result(task_id: int) -> str:
    """Full result (or error) of one background task."""
    from . import store
    t = store.get_task(task_id)
    if not t:
        return f"No task #{task_id}."
    out = f"#{t['id']} [{t['status']}] ({t['kind']}) {t['title']}\n"
    if t["error"]:
        out += f"ERROR: {t['error']}\n"
    if t["result"]:
        out += f"\n{t['result'][:4000]}"
    return out


async def approve_task(task_id: int) -> str:
    """Approve a teams_draft task — sends the drafted message to its Teams chat.
    Only call when Arun explicitly approves."""
    from . import tasks
    try:
        return await tasks.approve(task_id)
    except ValueError as exc:
        return str(exc)


async def ship_task(task_id: int) -> str:
    """Push a finished code task's branch(es) and open a PR per repo it touched.

    Use ONLY when Arun says to raise/ship the PR — a task finishing is not permission
    to publish. The pipeline never pushes on its own; this is the only path."""
    from . import tasks
    try:
        return await tasks.ship(task_id)
    except (ValueError, RuntimeError) as exc:
        return f"Ship failed: {exc}"


async def reject_task(task_id: int) -> str:
    """Reject a task: stops a running worker (and its spend), discards a draft."""
    from . import tasks
    try:
        return await tasks.reject(task_id)
    except ValueError as exc:
        return str(exc)


async def teams_read_chat(chat: str, limit: int = 15) -> str:
    """Read the last messages of a Teams chat by person/group name (e.g. 'Vinish').
    Uses Arun's logged-in Teams web session — deterministic browser automation, ~10-20s."""
    from . import teams_bridge
    if not teams_bridge.enabled():
        return "Teams bridge is off (set TEAMS_BRIDGE=1 in .env)."
    if not teams_bridge.logged_in_once():
        return "Not logged in — Arun must run: .venv/bin/python -m app.teams_bridge login"
    try:
        msgs = await teams_bridge.read_chat(chat, limit)
        return untrusted.wrap_lines(msgs, f"Teams chat: {chat}") or f"No messages found in chat '{chat}'."
    except RuntimeError as exc:
        if "SESSION_EXPIRED" in str(exc):
            return "Teams session expired — Arun must rerun: python -m app.teams_bridge login"
        return f"Teams read failed: {exc}"


async def teams_activity(limit: int = 25) -> str:
    """Read Arun's Teams Activity feed — who mentioned him, replies, missed calls, invites.
    Use whenever he asks anything like 'any messages for me', 'anything from Vinish',
    'what did I miss', 'any mentions'. Reads Teams directly (not macOS notifications),
    so muted chats and silenced notifications are still covered. Takes ~15-25s."""
    from . import teams_bridge
    if not teams_bridge.enabled():
        return "Teams bridge is off (set TEAMS_BRIDGE=1 in .env)."
    if not teams_bridge.logged_in_once():
        return "Not logged in — Arun must run: .venv/bin/python -m app.teams_bridge login"
    try:
        items = await teams_bridge.read_activity(limit)
        return untrusted.wrap_lines(items, "Teams activity feed") or "Activity feed is empty."
    except RuntimeError as exc:
        if "SESSION_EXPIRED" in str(exc):
            return "Teams session expired — Arun must rerun: python -m app.teams_bridge login"
        return f"Teams activity read failed: {exc}"


async def outlook_mail(limit: int = 15, only_needing_attention: bool = False) -> str:
    """Read Arun's Outlook inbox (read-only). Use for 'any mails for me', 'anything in
    my inbox', 'any mail needing my attention'. Set only_needing_attention=True to skip
    alerts/newsletters and show unread mail from real people. Takes ~20-30s."""
    from . import outlook, teams_bridge
    if not (teams_bridge.enabled() and teams_bridge.logged_in_once()):
        return "Outlook needs the Teams web session — Arun must run: python -m app.teams_bridge login"
    try:
        mails = await outlook.read_mail(limit)
        if only_needing_attention:
            mails = outlook.needs_attention(mails)
        return untrusted.wrap_lines([outlook.fmt_mail(m) for m in mails], "Outlook inbox") or "Nothing matching in the inbox."
    except RuntimeError as exc:
        if "SESSION_EXPIRED" in str(exc):
            return "Outlook session expired — Arun must rerun: python -m app.teams_bridge login"
        return f"Outlook read failed: {exc}"


async def outlook_meetings() -> str:
    """Today's meetings from Arun's Outlook calendar. Use for 'what meetings do I have',
    'am I free at 3', 'what's on my calendar'. Takes ~20-30s."""
    from . import outlook, teams_bridge
    if not (teams_bridge.enabled() and teams_bridge.logged_in_once()):
        return "Outlook needs the Teams web session — Arun must run: python -m app.teams_bridge login"
    try:
        items = await outlook.todays_meetings()
        return untrusted.wrap_lines(items, "Outlook calendar") or "Nothing on the calendar today."
    except RuntimeError as exc:
        if "SESSION_EXPIRED" in str(exc):
            return "Outlook session expired — Arun must rerun: python -m app.teams_bridge login"
        return f"Calendar read failed: {exc}"


async def teams_send_message(chat: str, text: str, to_group: bool = False) -> str:
    """Send a Teams message as Arun, to a PERSON's 1:1 chat.

    "ping Vinish" ALWAYS means Vinish's personal one-to-one chat — never a group or
    channel that happens to have his name in it. Only set to_group=True when Arun
    named the group/channel himself. Only send when he explicitly asked; confirm the
    wording first unless he dictated it. Returns the chat the message landed in."""
    from . import teams_bridge
    if not teams_bridge.enabled():
        return "Teams bridge is off (set TEAMS_BRIDGE=1 in .env)."
    if not teams_bridge.logged_in_once():
        return "Not logged in — Arun must run: .venv/bin/python -m app.teams_bridge login"
    try:
        where = await teams_bridge.send_message(chat, text, allow_group=to_group)
        return f"Sent — delivered to: {where}"
    except RuntimeError as exc:
        if "SESSION_EXPIRED" in str(exc):
            return "Teams session expired — Arun must rerun: python -m app.teams_bridge login"
        return f"Teams send failed: {exc}"


def build_agent(selected: list[str] | tuple[str, ...] | None = None) -> Agent:
    """The chat agent.

    Tools come from the capability registry, never from a list written here — the
    same table teaches the CLI brains and the MCP server, so a capability is
    described exactly once. `selected` narrows the toolset for one message (see
    tool_index); None keeps all of them.
    """
    from . import capabilities
    return Agent(tools=capabilities.tools_for(selected), retries=1)
