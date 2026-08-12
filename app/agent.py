"""The Asta agent: one PydanticAI agent, swappable models, memory + workspace tools."""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import re as _re
import time

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from . import memory, skills, store, untrusted, workspace_tools

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

Working loop: you don't have to stop and wait for Arun between steps. When a task isn't finished
and you already know the next step, call continue_working(next_step) as your LAST action — Asta
runs it immediately, no message needed — and keep going until the work is genuinely done. The one
exception is anything that leaves this chat: a Teams reply, an email, a Jira comment, a PR body, a
message to a person. NEVER send those directly — draft the content and call prepare_to_send, and
Asta will show Arun the draft and ask "can I send this?" before anything goes out. Stop the loop
only when the task is complete or you truly need his decision.

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
# Per-brain TRAITS live here, in one table, so a brain's differences are DECLARED
# DATA rather than scattered `if model == …` branches — the whole point of the
# consistency work. Adding a model is a row here plus its runner, nothing else.
#   identity  how its operating prompt is delivered: "system" (a real system
#             prompt — CLI --append-system-prompt, or PydanticAI instructions) or
#             "prefix" (folded into the user message; Copilot has no system flag).
#   tools     how it reaches Asta's capabilities: "mcp" (CLI brains, native
#             mcp__asta__* forwarding to /api/_invoke) or "in_process" (the agent
#             calls the Python functions directly).
#   context   approximate usable context window — the budget the token/local work
#             reads to decide how many tool schemas to expose. Local is the small
#             one, and therefore the forcing function for leanness.
#   rank      failover order, lowest tried first: subscription CLIs (no marginal
#             $, strong) → free local → metered API keys last.
_SPECS: dict[str, dict] = {
    # Office-paid workhorse, default for day-to-day chat.
    "copilot":    {"kind": "cli", "runner": "copilot_cli", "executes": True,  "effort": True,
                   "exec_name": "copilot",
                   "identity": "prefix", "tools": "mcp", "context": 128000, "rank": 10,
                   "label": "Copilot CLI (office)", "hint": "install/auth: copilot login"},
    # Runs on the Claude subscription Arun already pays for — listed above the
    # API-key "claude" entry, which bills a second prepaid account for the same model.
    # exec_name: background tasks have called this executor "claude" since before
    # it was a chat option, and that string is persisted in kv rows — so the
    # table carries both names rather than migrating live data.
    "claude_cli": {"kind": "cli", "runner": "claude_cli", "executes": True,  "effort": True,
                   "exec_name": "claude",
                   "identity": "system", "tools": "mcp", "context": 200000, "rank": 20,
                   "label": "Claude CLI (subscription)", "hint": "install/auth: claude login"},
    "claude":     {"kind": "api", "env": "ANTHROPIC_API_KEY", "executes": False, "effort": False,
                   "identity": "system", "tools": "in_process", "context": 200000, "rank": 40,
                   "model_env": ("ASTA_CLAUDE_MODEL", "claude-sonnet-5"), "label": "Claude"},
    "openai":     {"kind": "api", "env": "OPENAI_API_KEY", "executes": False, "effort": True,
                   "identity": "system", "tools": "in_process", "context": 128000, "rank": 50,
                   "model_env": ("ASTA_OPENAI_MODEL", "gpt-4o"), "label": "OpenAI"},
    "local":      {"kind": "api", "env": "", "executes": False, "effort": False,
                   "identity": "system", "tools": "in_process", "context": 8192, "rank": 30,
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


#: Trait defaults for a brain the table hasn't fully described yet — safe,
#: conservative values so a half-declared model still behaves.
_TRAIT_DEFAULTS = {"identity": "prefix", "tools": "in_process", "context": 8192, "rank": 999}


def brain_traits(name: str) -> dict:
    """The declared traits of a brain (identity/tools/context/rank), one lookup
    for every caller that needs to know how a model differs — so those
    differences stay DATA in the spec table, never re-hardcoded at the call site."""
    s = spec(normalize_model(name))
    return {k: s.get(k, d) for k, d in _TRAIT_DEFAULTS.items()}


def fallback_order() -> list[str]:
    """Brains in the order a dried-up turn should try them — lowest rank first
    (subscription CLIs → free local → metered API keys). Derived from the trait
    table so adding a model to the chain is a `rank` value, not a code edit."""
    return [n for _, n in sorted((brain_traits(n)["rank"], n) for n in _SPECS)]


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


# A subscription brain that has run out is INSTALLED and USELESS, and `available()`
# only ever answered the first half. So `default_chat_model()` kept handing every
# new conversation to Copilot after its monthly quota was gone — the switch had to
# be made by hand, per conversation, for a brain that could not answer any of them.
#
# The cooldown is how it comes back on its own: Copilot's pool is monthly and we
# are not told the reset date, so it is retried every few hours rather than being
# written off; Claude's is a rolling five-hour window, so it matches that window.
# One table, consulted by every caller — a per-brain constant somewhere else is
# how two parts of this file end up disagreeing about who is up.
QUOTA_COOLDOWN = {"copilot": 6 * 3600, "claude_cli": 5 * 3600}


def quota_kv(name: str) -> str:
    return f"{name}_quota_down"


def mark_quota_down(name: str) -> None:
    store.kv_set(quota_kv(name), str(time.time()))


def quota_down(name: str) -> bool:
    """True while `name` is known to be out of quota and not yet worth retrying."""
    raw = store.kv_get(quota_kv(name))
    if not raw:
        return False
    try:
        since = float(raw)
    except (TypeError, ValueError):
        return False
    if time.time() - since < QUOTA_COOLDOWN.get(name, 3600):
        return True
    store.kv_del(quota_kv(name))     # cooldown served — let it prove itself again
    return False


# A brain stops for one of two reasons that look alike in an error string but
# want opposite handling. It CRASHED — a bug, a bad prompt, a dead session — and
# resuming just repeats the failure. Or it hit a usage ceiling that lifts on its
# own: Copilot's monthly quota, Claude's rolling session window, an API rate
# limit. The second is a PAUSE, not a failure — keep the work, come back when it
# clears. One classifier, so every brain is paused and resumed by the same rule
# rather than each caller inventing its own substring test (which is exactly how
# "session limit" slipped past a test that only looked for "quota").
_TRANSIENT_LIMIT_MARKERS = (
    "quota",
    "session limit",
    "usage limit",
    "rate limit", "rate-limit", "ratelimit",
    "too many requests",
    "overloaded",
    "limit reached", "reached your limit", "hit your limit", "hit your session",
    "resets ", "resets at", "try again later",
)


def transient_limit(msg: str) -> bool:
    """True when a brain stopped on a temporary usage/rate/session limit — a
    'come back later' condition rather than a crash. A drained PREPAID balance
    ('credit balance is too low') is excluded: that account does not self-heal,
    so pausing to wait for it would wait forever."""
    m = (msg or "").lower()
    if "credit balance" in m:
        return False
    return any(k in m for k in _TRANSIENT_LIMIT_MARKERS)


_RESET_TIME = _re.compile(
    r"reset[s]?(?:\s+(?:at|on))?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", _re.I)


def limit_reset_at(msg: str, now: float | None = None) -> float | None:
    """Best-effort epoch when a limit message says it lifts, else None. Claude
    prints e.g. 'resets 3:40pm (Asia/Calcutta)'; the server runs on that same
    machine, so its local clock is the right one to read the time against."""
    m = _RESET_TIME.search(msg or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    base = _dt.datetime.now() if now is None else _dt.datetime.fromtimestamp(now)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= base:                       # the named time is later today, or tomorrow
        target += _dt.timedelta(days=1)
    return target.timestamp()


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
        if ok and quota_down(name):
            ok, hint = False, "quota exhausted — it'll be retried automatically"
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
    """Day-to-day default: the cheapest CLI subscription that can actually answer.

    Order is by what it costs Arun, not by preference: Copilot is office-paid, so
    it goes first — but only while it has quota. `model_registry()` folds that in,
    so a brain that is installed and exhausted no longer counts as available and
    the next one takes over on its own.
    """
    reg = model_registry()
    for name in EXECUTORS:
        if reg.get(name, {}).get("available"):
            return name
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


def thinking_budget() -> int:
    """Extended-thinking budget for the API path, in tokens. 0 = off (the default).

    Thinking is deliberately opt-in: it buys deeper reasoning but bills the thinking
    tokens on every turn, which fights the token-wastage goal — so it is off unless
    Arun turns it on. ASTA_THINKING accepts on|1 (a modest 2048 default) or an
    explicit token budget. CLI brains reason via the effort ladder instead, so this
    only affects the in-process (Claude API) brain."""
    raw = os.environ.get("ASTA_THINKING", "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return 0
    if raw in ("1", "true", "yes", "on"):
        return 2048
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def model_settings(name: str):
    if name == "claude":
        # Cache the stable prefix (instructions + tool defs) -> ~90% cheaper repeat turns.
        kw = dict(anthropic_cache_instructions=True,
                  anthropic_cache_tool_definitions=True)
        budget = thinking_budget()
        if budget:
            kw["anthropic_thinking"] = {"type": "enabled", "budget_tokens": budget}
        return AnthropicModelSettings(**kw)
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


#: A description shorter than this explains nothing on its own — "see comments",
#: a bare link, a copied error string. Not a bug to fix in Jira, just how tickets
#: get written when the reporter is in a hurry.
_THIN_DESCRIPTION = 80


def _reads_thin(text: str) -> bool:
    return len("".join((text or "").split())) < _THIN_DESCRIPTION


async def jira_issue(key: str, comments: int = 10) -> str:
    """Full detail of one Jira issue: status, description, AND the comment thread.

    The comments are not background colour. On plenty of tickets the description is
    one line and the real requirement was settled in the Q&A underneath it, so
    answering from the title and description alone produces confident nonsense.
    Raise `comments` when the recent thread refers back to something older.
    """
    from . import jira
    i = await jira.get_issue(key, comment_limit=max(1, comments))
    thread, total = i.get("comments") or [], i.get("comment_total", 0)
    head = (f"{i['key']} [{i['type']} / {i['status']} / {i['priority']}] {i['summary']}\n"
            f"Labels: {', '.join(i['labels']) or '-'} Components: {', '.join(i['components']) or '-'}")

    # Say when the ticket does not explain itself. Without this the model reads a
    # one-line description, finds nothing contradicting its first guess, and
    # answers with confidence it has not earned. Naming the gap is what turns
    # that into a question for Arun.
    if _reads_thin(i["description"]):
        head += ("\nNOTE: this ticket's description does not stand on its own — "
                 + ("read the comments below for what is actually being asked."
                    if thread else
                    "and there are no comments either. Do not infer the requirement "
                    "from the title; ask Arun what it means."))

    body = [f"--- description ---\n{i['description'].strip() or '(empty)'}"]
    if thread:
        shown = (f"showing the {len(thread)} most recent of {total}"
                 if total > len(thread) else f"all {len(thread)}")
        lines = [f"[{c['created'][:10]}] {c['author']}: {c['text']}".rstrip()
                 for c in thread]
        body.append(f"--- comments ({shown}, oldest first) ---\n" + "\n\n".join(lines))
        if total > len(thread):
            body.append(f"({total - len(thread)} older comment(s) not shown — call "
                        f"jira_issue('{i['key']}', comments={total}) if the thread "
                        f"refers back to something missing.)")
    else:
        body.append("--- comments ---\n(none)")

    # Anyone with tracker access can write the summary, description and comments,
    # so the whole lot is untrusted — one fence around all of it.
    return head + "\n\n" + untrusted.wrap("\n\n".join(body), f"Jira {i['key']}")


async def jira_sprint() -> str:
    """What Arun has committed to in the CURRENT sprint — the board, not the backlog.

    Different from jira_my_issues, which is everything assigned and not done and happily
    includes work from three sprints ago. Use for 'what's on me this sprint', standup, and
    before offering to pick something up."""
    from . import jira
    try:
        issues = await jira.current_sprint()
    except RuntimeError as exc:
        return str(exc)
    if not issues:
        return "Nothing assigned to you in the open sprint."
    return "\n".join(f"{i['key']} [{i['status']}] {i['summary']}" for i in issues)


async def jira_comment(key: str, text: str) -> str:
    """Propose a comment on a Jira issue. Write the exact wording you mean to post.

    This does NOT post. It stages the comment and asks Arun; his yes posts the exact text
    you wrote here, unchanged. So write it as the finished comment, not as a description of
    one, and tell him in your reply that it is waiting for his go-ahead."""
    from . import jira, offers
    if not jira.configured():
        return "Jira is not configured — set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env"
    offers.staged_write(
        "jira_comment", {"key": key, "text": text},
        f"💬 Comment on {key}", text.strip()[:900],
        f"Post this comment on {key}?", kind="jira_write")
    return f"Staged the comment on {key} — waiting for Arun's yes. Nothing posted yet."


async def jira_transition(key: str, to_status: str) -> str:
    """Propose moving a Jira issue to another status (e.g. 'In Progress', 'Ready for Retest').

    This does NOT move it. The valid targets are checked now, so an impossible status fails
    here rather than after he has approved it; then the move is staged for his yes."""
    from . import jira, offers
    if not jira.configured():
        return "Jira is not configured — set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env"
    try:
        valid = await jira.list_transitions(key)
    except Exception as exc:
        return f"Could not read {key}'s workflow: {exc}"
    if not any(v.lower() == to_status.strip().lower() for v in valid):
        return (f"{key} cannot move to '{to_status}' — valid targets: "
                f"{', '.join(valid) or 'none'}. Offer those to Arun.")
    offers.staged_write(
        "jira_transition", {"key": key, "to_status": to_status},
        f"✏️ Move {key} → {to_status}", f"Status change on {key}.",
        f"Move {key} to {to_status}?", kind="jira_write")
    return f"Staged the move of {key} to {to_status} — waiting for Arun's yes."


async def pr_review_post(pr: str, action: str, body: str = "",
                         workspace: str = "", repo: str = "") -> str:
    """Propose posting a review on a pull request. Only when Arun asked you to.

    action: 'approve', 'comment', or 'request_changes'. body: the review text, written as
    the finished comment — his yes posts exactly these words under his name, so write them
    for the PR author to read, not for Arun.

    This does NOT post. Approving someone's change is visible to the whole team the moment
    it lands, so it always waits for his explicit yes. Use review_pr first to form the
    opinion; use this only when he says to post it."""
    from . import offers, review
    if action not in review.ACTIONS:
        return f"action must be one of: {', '.join(sorted(review.ACTIONS))}"
    if action != "approve" and not (body or "").strip():
        return f"a '{action}' review needs a body — write the comment first"
    verb = {"approve": "Approve", "comment": "Comment on",
            "request_changes": "Request changes on"}[action]
    offers.staged_write(
        "pr_review", {"pr": pr, "workspace": workspace, "repo": repo,
                      "action": action, "body": body},
        f"🔎 {verb} PR #{str(pr).lstrip('#')}",
        (body or "(no comment body — approval only)").strip()[:900],
        f"{verb} PR #{str(pr).lstrip('#')} as you?", kind="pr_write")
    return f"Staged the {action.replace('_', ' ')} on PR #{str(pr).lstrip('#')} — waiting for Arun's yes."


async def teams_status(status: str = "") -> str:
    """Read or set Arun's Teams presence.

    status: leave empty to read it; or one of available / busy / do not disturb / be right
    back / away / offline (he also says 'dnd', 'brb', 'free'). Do it when he asks — this is
    his own status, not a message to anyone — but say what it reads afterwards, because a
    status he thinks is DND and isn't will cost him the next hour."""
    from . import teams_bridge
    if not teams_bridge.enabled():
        return "Teams bridge is off — " + teams_bridge.status()["hint"]
    try:
        if not (status or "").strip():
            now = await teams_bridge.read_presence()
            return f"Teams status: {now}" if now else "Couldn't read your Teams status."
        return f"Teams status is now {await teams_bridge.set_presence(status)}."
    except RuntimeError as exc:
        return f"Didn't change it — {exc}"


async def create_meeting(subject: str, when: str, minutes: int = 30,
                         attendees: str = "", agenda: str = "") -> str:
    """Propose a meeting invite. Does NOT send it.

    when: 'YYYY-MM-DD HH:MM' in his local time — resolve 'Thursday at 3' to an actual date
    yourself before calling, and if you are not sure which day he means, ask. attendees:
    comma-separated email addresses.

    The invite is built and staged; his yes sends it. An invite books time in other
    people's calendars, so it never goes out on your judgement alone."""
    from . import meetings, offers
    try:
        invite = meetings.meeting_invite(
            subject, when, minutes,
            [a for a in (attendees or "").split(",") if a.strip()], agenda)
    except RuntimeError as exc:
        return f"Can't build that invite — {exc}"
    offers.staged_write(
        "calendar_send", {"url": invite["url"], "summary": invite["subject"]},
        "📅 Meeting invite", meetings.describe(invite),
        "Send this invite?", kind="calendar")
    return f"Staged the invite — waiting for Arun's yes.\n{meetings.describe(invite)}"


async def request_leave(start_date: str, end_date: str = "", reason: str = "",
                        to: str = "") -> str:
    """Propose an all-day leave / out-of-office invite. Does NOT send it.

    start_date and end_date: 'YYYY-MM-DD', both inclusive — one day off means passing the
    same date twice or leaving end_date empty. to: comma-separated addresses (his manager,
    his team). Staged for his yes, because this one goes to the people who approve it."""
    from . import meetings, offers
    try:
        invite = meetings.leave_invite(
            start_date, end_date, reason,
            [a for a in (to or "").split(",") if a.strip()])
    except RuntimeError as exc:
        return f"Can't build that leave request — {exc}"
    offers.staged_write(
        "calendar_send", {"url": invite["url"], "summary": invite["subject"]},
        "🌴 Leave request", meetings.describe(invite),
        "Send this leave invite?", kind="calendar")
    return f"Staged the leave request — waiting for Arun's yes.\n{meetings.describe(invite)}"


async def join_meeting_by_name(which: str) -> str:
    """Join a meeting Arun names rather than links — "join my 3pm", "join the standup".

    Use this whenever he refers to a meeting by time or by name; only fall back to
    join_meeting when he actually pastes a link. If the phrase does not pick out
    exactly one meeting this refuses and lists the day — pass that back to him and
    ask which he meant. Never guess: joining the wrong call puts him in a room in
    front of people who watch him arrive."""
    import asyncio as _asyncio

    from . import meetings
    try:
        result = await meetings.join_by_phrase(which)
    except RuntimeError as exc:
        return f"Didn't join — {exc}"
    _asyncio.create_task(meetings.watch_and_report(which))
    return result


async def join_meeting(join_url: str, title: str = "") -> str:
    """Join a Teams meeting from its join link, muted with the camera off.

    Use when Arun says to sit in on a call he cannot attend. Joining is listening only —
    to actually say something you need say_in_call, which is separate and usually not
    available. Tell him he is joined and that you are only listening.

    Asta stays in the call and hangs up by itself when it ends, then offers to pull out
    anything that concerned him. Don't wait for that: reply to him now."""
    import asyncio as _asyncio

    from . import meetings
    try:
        result = await meetings.join(join_url)
    except RuntimeError as exc:
        return f"Didn't join — {exc}"
    # The watcher outlives this turn on purpose — a meeting lasts far longer than
    # any reasonable turn, and holding the conversation open for it would make the
    # whole assistant unresponsive for an hour.
    _asyncio.create_task(meetings.watch_and_report(title))
    return (f"{result}. I'll stay on and hang up when it ends, then offer you "
            f"anything from it that's yours. I'm only listening — I won't speak.")


async def leave_meeting() -> str:
    """Hang up on the call Asta is sitting in. Use when Arun says to drop off."""
    from . import meetings
    return await meetings.leave()


async def meeting_notes() -> str:
    """The transcript Asta captured from the last call it sat in on.

    Use for "what did I miss", "notes from that call", "what was said". This is live
    captions read out of Teams while the call ran — real speech recognition of a real
    meeting, so it is imperfect and it only covers the part Asta was present for.
    Summarise from it; do not fill in gaps it does not contain."""
    from . import meetings
    text = meetings.captured_transcript() or meetings.last_transcript()
    if not text:
        return ("No captions were captured — either Asta wasn't in the call, or live "
                "captions could not be turned on. Nothing to summarise; if the meeting "
                "was recorded, Teams' own transcript is the place to look.")
    live = " (call still running — this is what has been said so far)" \
        if meetings.captured_transcript() else ""
    return f"Captured transcript{live}:\n\n" + untrusted.wrap(text, "Teams live captions")


async def say_in_call(text: str) -> str:
    """Say something out loud in the call, in Arun's voice. ONLY when he gave you the words.

    Never improvise in a live call and never answer a question on his behalf: say what he
    told you to say, nothing else. Usually unavailable — it needs a virtual microphone
    configured on the machine — and when it is, this returns the reason rather than
    pretending something was said."""
    from . import meetings
    try:
        return await meetings.say_in_call(text)
    except RuntimeError as exc:
        return f"Said nothing — {exc}"


def watch_ci(what: str, repo: str = "") -> str:
    """Also watch a build that isn't Arun's own work — a release branch, a workflow, a repo.

    By default the CI watcher only reports runs he triggered and pipelines on PRs he
    authored, which is what keeps it quiet enough to be worth reading. Use this when he
    says 'keep an eye on the release build' or names a specific pipeline. Prefix with
    'stop ' to unsubscribe."""
    from . import ci_watch
    what = (what or "").strip()
    if what.lower().startswith("stop "):
        return ci_watch.unwatch(what[5:])
    return ci_watch.watch(what, repo)


def propose_next(next_step: str, why: str = "") -> str:
    """Offer Arun a next step and stop, instead of doing it or ending the conversation.

    This is how any flow continues: you have finished a piece of work, there is an obvious
    next move, and it is his call whether you take it. Say what you would do, concretely
    enough that 'yes' is unambiguous — 'implement PROJ-412 on a branch and run the tests',
    not 'continue'. He can answer from his phone hours later and it will still run.

    next_step: what you would do, as an instruction to yourself.
    why: one line on why it is the right next move.

    Use it for: after analysing a bug, after reading a ticket, after a follow-up lands,
    after a review — anywhere you would otherwise ask 'shall I?' in prose and lose it when
    the turn ends. Do NOT use it for something you can just do, or for a message leaving
    this chat (that is prepare_to_send)."""
    from . import offers
    step = (next_step or "").strip()
    if not step:
        return "propose_next needs a concrete next step."
    offers.propose(subject="▶ Next step", context=(why or "").strip(),
                   question=step, action=step)
    return ("Offered it to Arun — his yes runs it, from any channel. "
            "Now finish your reply; do not do the step yourself.")


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


def token_audit(hours: float = 24) -> str:
    """Where recent worker sessions actually BURNED tokens, ranked, with the fix for
    each and whether waste is trending down. Reads the executors' own session logs and
    classifies avoidable spend (duplicate/full reads, fat cache-amplified outputs, over-
    grepping, narration, re-plan re-caching) — no model call. Use when Arun asks where
    tokens are going or how to make a run leaner; trace_report is the raw cost, this is
    the diagnosis + the fix."""
    from . import token_audit as ta
    rep = ta.audit_recent(hours)
    if not rep.get("sessions_audited"):
        return (f"No worker sessions to audit in the last {hours:g}h. "
                "Waste analysis runs on delegated code/analysis tasks; run one and ask again.")
    lines = [f"Token audit — {rep['sessions_audited']} worker sessions, waste ratio "
             f"{rep['aggregate_waste_ratio']:.1%} (~{rep['aggregate_avoidable_tokens']:,} "
             f"avoidable tokens). Trend: {rep['trend_vs_previous']}."]
    if rep.get("top_fix_categories"):
        lines.append("Top fixes (most avoidable tokens first):")
        for cat, tok in rep["top_fix_categories"]:
            lines.append(f"  • {cat}: ~{tok:,} tok")
    return "\n".join(lines)


async def set_reminder(text: str, due_iso: str, repeat: str = "") -> str:
    """Set a reminder. due_iso: LOCAL time ISO format e.g. '2026-07-19T15:00'.
    repeat: '' (once) | daily | weekdays | weekly. It fires to whichever phone
    channel is connected, and always to the in-app bell. Do NOT promise WhatsApp
    or Telegram yourself — this reply states which channels are actually live."""
    from . import reminders, notify
    try:
        r = reminders.create(text, due_iso, repeat)
    except ValueError as exc:
        return f"Couldn't set reminder: {exc}"
    import datetime as dt
    when = dt.datetime.fromtimestamp(r["due_at"]).strftime("%a %d %b %H:%M")
    head = f"Reminder #{r['id']} set for {when}" + (f" (repeats {repeat})" if repeat else "")
    # Tell the truth about delivery now, so a reminder can't silently fire into a
    # channel that is down. This is the fix for "it writes but doesn't send".
    live = await notify.live_push_channels()
    if live:
        return f"{head} — I'll ping you on {' and '.join(live)}."
    return (f"{head}. ⚠️ No phone channel is connected right now, so it will only "
            f"show in the app. Link WhatsApp or Telegram to get it on your phone.")


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


def continue_working(next_step: str) -> str:
    """Keep working autonomously instead of ending the turn and waiting for Arun.

    Call this as the LAST thing you do in a turn when the task is NOT finished and you
    already know the next step — you have what you need to proceed, or you're mid-task.
    `next_step` is one line naming what you'll do next; Arun sees it as the loop's
    thinking, and Asta runs that step immediately without waiting for a message. Do NOT
    use it to send anything outward (use prepare_to_send), and do NOT call it when the
    work is actually done or you genuinely need his decision."""
    from . import tasks, loop
    cid = tasks.current_conversation()
    if not cid:
        return "No active conversation to continue — finish this turn normally."
    loop.set_continue(cid, next_step)
    return f"Continuing automatically: {next_step or 'next step'}"


def prepare_to_send(what: str, to: str = "", channel: str = "chat",
                    to_group: bool = False) -> str:
    """Stage an outward-facing message for Arun to approve BEFORE it is sent.

    Use this whenever you've drafted something to send outside this chat — a Teams
    reply, an email, a Jira comment, a PR description, a message to a person. `what` is
    the full draft, `to` the recipient/target, `channel` one of teams|email|jira|pr|chat.
    Asta shows Arun the draft and asks "can I send this?" — it is NEVER sent until he
    confirms. This is the ONLY approved way to send on his behalf; never send outward
    through any other tool without staging it here first.

    `to` on Teams means a PERSON's 1:1 chat. Set to_group=True ONLY when Arun named a
    group or channel himself ("post it in the prod issue group") — never because a
    group happens to share a word with the name he used."""
    from . import tasks, loop
    cid = tasks.current_conversation()
    if not cid:
        return "No active conversation — cannot stage a send."
    loop.set_pending_send(cid, what, to, channel, to_group=to_group)
    tgt = f" to {'group ' if to_group else ''}{to}" if to else ""
    return f"Draft staged{tgt} on {channel}. Asking Arun to confirm before it's sent."


def delegate_task(title: str, prompt: str, kind: str = "analysis",
                  workspace: str = "", teams_chat: str = "") -> str:
    """Spawn a background worker so the chat stays free; Arun gets a WhatsApp/Telegram
    notification with the result. The prompt must be SELF-CONTAINED (the worker has no
    chat context). kind: analysis (read-only, parallel) | code (edits code — set
    the workspace) | teams_draft (drafts a Teams reply — set teams_chat; the
    draft waits for Arun's approval, it is never sent automatically)."""
    from . import relevance, tasks
    # A question is not a request to go do work. If this turn was opened by a
    # passive question and the model is now trying to spawn work off it, hold and
    # ask first rather than silently running (and touching a repo) unasked.
    held = relevance.guard_spawn(kind, title, workspace)
    if held:
        return held
    # Feedback on work that just finished is not a new task, however much it
    # reads like one. Spawning here is what made Arun's corrections start from
    # nothing: a fresh session, none of the context of the change being
    # criticised, and a second implementation of the same thing.
    same = tasks.refinable_match(title, prompt, workspace)
    if same:
        return (f"This looks like feedback on task #{same['id']} "
                f"(“{same['title'][:60]}”, {same['status']}), not a new piece of "
                f"work. Call refine_task({same['id']}, \"<what should change>\") "
                f"so it continues in that task's own session with everything it "
                f"already knows. If it really IS unrelated new work, say so and "
                f"spawn it with a title that does not restate the old one.")
    t = tasks.spawn(title, prompt, kind, workspace or None, teams_chat)
    return (f"Task #{t['id']} ({kind}) spawned — running in the background. "
            f"Arun will be notified when it finishes.")


async def refine_task(task_id: int, feedback: str) -> str:
    """Continue a FINISHED code task with Arun's feedback, in its own session.

    Use this — never delegate_task — whenever he comments on work a task already
    delivered: a correction, an addition, "also handle X", a review comment, or
    a CI failure on its PR. The task keeps everything it learned; a new task
    would start from nothing and re-implement what is already there.
    Works on tasks that are done, shipped, failed, or blocked on their PR."""
    from . import tasks
    try:
        return await tasks.refine(task_id, feedback)
    except ValueError as exc:
        return str(exc)


def task_pr_status(task_id: int = 0) -> str:
    """Where the PRs for shipped tasks stand — CI, review, merged or not.
    Call with 0 for every task whose PR is still open."""
    from . import store, tasks
    ids = [task_id] if task_id else tasks.open_prs()
    if not ids:
        return "No task has an open PR right now."
    lines = []
    for tid in ids:
        t = store.get_task(tid)
        if not t:
            continue
        checked = t.get("pr_checked_at")
        when = (f", checked {int((time.time() - checked) / 60)}m ago"
                if checked else ", not checked yet")
        lines.append(f"#{tid} {t['title'][:50]} — {t['status']} "
                     f"[{t.get('pr_state') or 'unknown'}{when}]")
        for url in (t.get("pr_urls") or "").splitlines():
            lines.append(f"    {url}")
    return "\n".join(lines)


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


async def teams_history(chat: str, when: str = "last night", limit: int = 60) -> str:
    """Read a Teams chat for a TIME WINDOW — 'what did Vinish say last night',
    'anything from Suraj yesterday', 'messages from the triage group this morning'.
    `when` is plain English: last night, yesterday, this morning, today, last week,
    'last 3 hours', or 'while I was away'. Use this instead of teams_read_chat
    whenever the question has a WHEN in it; teams_read_chat only sees what is
    currently on screen and cannot reach last night's messages."""
    from . import teams_bridge, when as when_mod
    if not teams_bridge.enabled():
        return "Teams bridge is off (set TEAMS_BRIDGE=1 in .env)."
    if not teams_bridge.logged_in_once():
        return "Not logged in — Arun must run: .venv/bin/python -m app.teams_bridge login"

    since, until, label = when_mod.parse(when)
    window = when_mod.describe(since, until)

    # Stored history first. Anything already read is answerable without opening
    # a browser, which turns a 20-second scrape into an instant answer for the
    # common case of asking twice about the same evening.
    rows = store.teams_messages(chat, since=since, until=until, limit=limit)
    source = "stored history"
    if not rows:
        try:
            fetched = await teams_bridge.read_history(chat, since=since, limit=limit)
        except RuntimeError as exc:
            if "SESSION_EXPIRED" in str(exc):
                return "Teams session expired — Arun must rerun: python -m app.teams_bridge login"
            return f"Teams read failed: {exc}"
        rows = [r for r in fetched if r.get("sent_at") and r["sent_at"] <= until]
        source = "Teams (scrolled back)"

    if not rows:
        return (f"Nothing found in '{chat}' for {label} ({window}). Asta scrolled the "
                f"thread back and read no message in that window — either none was sent, "
                f"or it is older than Teams will load.")

    lines = [teams_bridge.fmt_message(r) for r in rows]
    body = untrusted.wrap_lines(lines, f"Teams chat: {chat} — {label}")
    return f"{body}\n\n(window: {window}, from {source}; {len(rows)} message(s))"


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


def _pick_meeting(meetings: list[dict], title: str) -> dict | None:
    """The meeting to prep: a title match, else the next 'speaking' meeting (one where
    Arun has to say something), else just the next one."""
    from .briefing import _SPEAKING_MEETING
    if title.strip():
        t = title.strip().lower()
        return next((m for m in meetings if t in (m.get("title") or "").lower()), None)
    speaking = next((m for m in meetings if _SPEAKING_MEETING.search(m.get("title") or "")), None)
    return speaking or (meetings[0] if meetings else None)


def _prep_prompt(ev: dict, open_items: str) -> str:
    import re as _re
    org = f", organised by {ev['organizer']}" if ev.get("organizer") else ""
    is_11 = bool(_re.search(r"1[:-]?1|one[- ]on[- ]one|catch[- ]?up", ev.get("title", ""), _re.I))
    kind = "a 1:1" if is_11 else "a meeting"
    focus = ("Focus on his updates since last time, blockers to surface, and any asks or "
             "decisions he needs from the other person." if is_11 else
             "Focus on what he should raise, decisions needed, and risks to flag.")
    items = f"\nArun's open work items (for grounding):\n{open_items[:1500]}\n" if open_items.strip() else ""
    return (f"You are Arun's assistant, prepping him for {kind}.\n"
            f"Meeting: {ev.get('title', '')} at {ev.get('start', '')}{org}.{items}\n"
            "Write a tight prep as three short bulleted sections — **Talking points**, "
            f"**Questions to ask**, **Watch-outs**. No preamble. {focus}")


#: What "no prep" looks like. Deliberately a sentinel and not a template: the old
#: skeleton pushed three empty bullets under "(local model offline)" half an hour
#: before a meeting, which cost Arun a read and told him nothing he could use. A
#: form he has to fill in himself is not prep — it is the assistant handing the
#: work back with extra steps.
NO_PREP = ""


async def meeting_prep(title: str = "") -> str:
    """Draft prep for a meeting or 1:1 from today's calendar + your open work: talking
    points, questions to ask, and watch-outs. `title` matches a meeting by name; empty =
    the next meeting you have to speak in. Best-effort, local-model-first (cheap). This
    only DRAFTS — stage it with prepare_to_send if you want it sent to anyone."""
    from . import briefing
    try:
        meetings = await briefing._cached_meetings()
    except Exception as exc:
        return f"Couldn't read the calendar: {exc}"
    if not meetings:
        return "No meetings on today's calendar to prep."
    ev = _pick_meeting(meetings, title)
    if not ev:
        avail = ", ".join((m.get("title") or "?") for m in meetings[:6])
        return f"No meeting matching '{title}' today. On today: {avail}"
    open_items = ""
    try:
        open_items = await jira_my_issues()
    except Exception:
        pass
    # paid_ok: he walks into this meeting in half an hour. A short turn on a brain
    # he already pays for is worth it; an empty checklist is worth nothing.
    body = (await memory.cheap_complete(_prep_prompt(ev, open_items), 400,
                                        paid_ok=True) or "").strip()
    if not body:
        return NO_PREP
    org = f" (with {ev['organizer']})" if ev.get("organizer") else ""
    return f"📝 Prep — {ev.get('title', 'meeting')} at {ev.get('start', '')}{org}:\n\n{body}"


def _recap_needs_arun(body: str) -> bool:
    """True when the recap has an action item flagged for Arun."""
    return "ARUN:" in (body or "").upper()


async def meeting_recap(transcript: str, title: str = "") -> str:
    """Summarize a meeting/call transcript into a recap for Arun: TL;DR, decisions,
    action items (flagging any that need HIM), and open questions. Pass the transcript
    text — e.g. from Teams' own recording/recap. Use after a call he missed or wants
    summarized. Local-model-first (cheap). If an item needs Arun, he's pinged."""
    from . import notify
    t = (transcript or "").strip()
    if len(t) < 40:
        return ("Give me the transcript text (from Teams' recording/recap for the meeting) "
                "and I'll summarize it — decisions, action items, and anything that needs you.")
    prompt = ("Summarize this meeting transcript for Arun, who missed it. Four short "
              "sections: **TL;DR** (≤2 lines), **Decisions**, **Action items** (prefix any "
              "that are Arun's with 'ARUN:'), **Open questions**. Be concise, no preamble.\n\n"
              "TRANSCRIPT:\n" + t[:12000])
    # He asked for this and is waiting on it, so it may cost a paid turn — the
    # alternative was telling him to go and start LM Studio, which is not an answer.
    body = (await memory.cheap_complete(prompt, 700, paid_ok=True) or "").strip()
    if not body:
        return "No brain is available to summarize it — start LM Studio, or add a key in .env."
    head = f" — {title}" if title else ""
    recap = f"📋 Recap{head}:\n\n{body}"
    if _recap_needs_arun(body):
        with contextlib.suppress(Exception):
            await notify.notify(f"📋 A meeting recap needs you{head}:\n\n{body[:600]}",
                                "meeting", urgency="direct")
    return recap


async def draft_teams_reply(chat: str, question: str = "") -> str:
    """Draft a reply to a person's Teams question, grounded in Arun's memory + open work,
    for HIM to review. Reads the recent thread with `chat` for context. DRAFT ONLY — stage
    it with prepare_to_send (channel 'teams') so nothing goes out in his name without his yes.
    Use when someone pings Arun on Teams with a question and he wants a head start."""
    from . import teams_bridge
    thread: list[str] = []
    try:
        if teams_bridge.enabled() and teams_bridge.logged_in_once():
            thread = await teams_bridge.read_chat(chat, 12)
    except Exception:
        pass
    q = (question or "").strip() or "\n".join(thread[-6:])
    if not q.strip():
        return f"No question found in the thread with {chat}. Tell me what they asked."
    ground = ""
    with contextlib.suppress(Exception):
        ground = (memory.recall_block(q) or "")[:1000]
    prompt = (f"A colleague ({chat}) asked Arun this on Teams:\n{q}\n\n"
              + (f"Relevant context Arun has:\n{ground}\n\n" if ground else "")
              + "Draft Arun's reply in his voice — direct, brief, helpful. If you genuinely "
                "lack the information, say what you'd need instead of guessing. No greeting fluff.")
    draft = (await memory.cheap_complete(prompt, 400, paid_ok=True) or "").strip()
    if not draft:
        return f"No brain is available to draft it — answer {chat} manually for now."
    return f"✍️ Draft reply to {chat} (review, then say 'send' to post it):\n\n{draft}"


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


async def teams_resolve(chat: str, to_group: bool = False) -> str:
    """Check WHO a Teams message would actually reach, without sending anything.

    Use before sending when the name is short, common, or a surname ("Kumar", "Priya"),
    and whenever Arun names a group. It opens the same chat the send would open and
    reports its real title, so an ambiguous name is caught before a message lands on
    the wrong person rather than after."""
    from . import teams_bridge
    if not teams_bridge.enabled():
        return "Teams bridge is off (set TEAMS_BRIDGE=1 in .env)."
    try:
        r = await teams_bridge.resolve_target(chat, allow_group=to_group)
        kind = "group/channel" if to_group else "1:1 chat"
        return f"'{chat}' resolves to the {kind}: {r['opened']!r} — nothing was sent."
    except RuntimeError as exc:
        if "SESSION_EXPIRED" in str(exc):
            return "Teams session expired — Arun must rerun: python -m app.teams_bridge login"
        return f"Would NOT send: {exc}"


async def teams_call(who: str, video: bool = False) -> str:
    """Propose ringing someone on Teams. Only when Arun asked for a call.

    This does NOT dial. A call interrupts a person immediately and cannot be taken
    back, so it stages like any other outward act and waits for his yes. Reading a
    chat or sending a message is almost always the lighter thing to offer first."""
    from . import offers, teams_bridge
    if not teams_bridge.enabled():
        return "Teams bridge is off (set TEAMS_BRIDGE=1 in .env)."
    kind = "video call" if video else "call"
    offers.staged_write(
        "teams_call", {"who": who, "video": video},
        f"📞 {kind.title()} {who}", f"Teams {kind} to {who}.",
        f"Ring {who} on Teams?", kind="teams_write")
    return f"Staged the {kind} to {who} — waiting for Arun's yes. Nothing is ringing yet."


def build_agent(selected: list[str] | tuple[str, ...] | None = None) -> Agent:
    """The chat agent.

    Tools come from the capability registry, never from a list written here — the
    same table teaches the CLI brains and the MCP server, so a capability is
    described exactly once. `selected` narrows the toolset for one message (see
    tool_index); None keeps all of them.
    """
    from . import capabilities
    return Agent(tools=capabilities.tools_for(selected), retries=1)
