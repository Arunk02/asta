"""Which model, at what effort, for each leg of a task — chosen by the work.

Until this, a task went to Copilot, or to Claude when Copilot was out, at the
stage's fixed effort. The brain was chosen by availability and the effort by
the stage, never by what the work needed: a one-line fix and a three-repo
schema change got the same model at the same effort.

His table (19 Nov), Claude brain:

    tier                         plan                 build
    T1 · config value            haiku @ low          haiku @ low
    T1 · 1–2 files, cause known  haiku @ medium       haiku @ medium
    T2 · 3–10 files, a ticket    sonnet @ high        sonnet @ high
    T3 · risky-path change       opus @ medium        sonnet @ high
    T3 · big feature             opus @ high          sonnet @ high
    booking debugging            sonnet @ medium (a second pass @ high)

The size comes from WHAT THE PLAN CHANGES — the files in its STRUCTURE block,
config / test / code — never from words in its prose: 19 Nov, a two-line helm
change was "high impact (billing)" because the list it edited was named
…_TO_BILLING, and ran on opus twice. Before a plan exists, the size comes from
HIS words, not the brief a brain wrote for the task. After the build, the real
diff is checked against the tier (`fit`): work bigger than its tier moves the
remaining legs up one, and the miss is counted on the scorecard.

Brains stay sticky once a session exists (a session cannot move between them),
so a tier changes the model and effort of the brain the task is on. He can
override any of it in the message: "use claude", "cheap", "max".

Every decision is recorded (the task's event log and an outcome row), so the
scorecard can say what tier the work ran at and what each finished task cost.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from . import store

T1, T2, T3 = 1, 2, 3
NAMES = {T1: "T1", T2: "T2", T3: "T3"}


def enabled() -> bool:
    return os.environ.get("ASTA_ROUTING", "1").strip().lower() in ("1", "true", "yes", "on")


# --- what the words say ------------------------------------------------------------

#: More classes/files than this, or two repos, is a big feature: opus builds it.
BIG_FEATURE_FILES = 10
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,15}-\d{1,6}\b")
#: A change to a contract other code depends on: always worth the best model.
#: Deliberately not "contract" or "kafka topic" on their own: every change in his
#: services touches a topic, and a word that matches everything tiers nothing.
_CONTRACT = re.compile(
    r"\b(schema|migration|migrate|avro|dto|api contract|protobuf|"
    r"liquibase|flyway|ddl|alter table|breaking change)\b", re.I)
#: Work whose mistakes are expensive however small the diff: a Temporal workflow
#: (replay breaks running instances), money, and who may do what. Read from the
#: PLAN, where it names what the change touches. 15 Nov: a one-file workflow
#: change was tiered T1 by size alone.
_IMPACT = re.compile(
    r"\b(workflow\.getversion|getversion|replay|@workflowmethod|@signalmethod|"
    r"workflow (?:class|method|definition|code)|continue-?as-?new|"
    r"billing|invoic\w*|price owner|"
    r"authenticat\w*|authoris\w*|authoriz\w*|oauth|jwt|credentials?)\b", re.I)
_PROD_DOWN = re.compile(r"\b(prod(uction)? (is )?(down|broken|on fire)|outage|sev ?[12]|p1 incident)\b",
                        re.I)
_SMALL = re.compile(r"\b(typo|rename|one[- ]line|single line|log line|comment|bump|"
                    r"version bump|tiny|small fix|quick fix|wording|constant)\b", re.I)
#: A file named in a plan or an ask. The group is NON-capturing: with a capturing
#: one, findall returned the extension, so a plan naming five .java files counted
#: as ONE file — #272 was waved through as "1 class(es)/file(s)" and built at low
#: effort (9 Oct).
_FILE = re.compile(r"\b[\w./-]+\.(?:java|kt|py|ts|tsx|js|go|rb|cs|sql|yaml|yml|json|xml|md|gradle|properties)\b")

#: A config file: values, not logic. Helm values, application/bootstrap yml,
#: .properties, env files.
_CONFIG_PATH = re.compile(
    r"(?:^|/)(?:helm|charts?|k8s|deploy|config)/[^\s]*\.(?:ya?ml|properties|json|env)$"
    r"|(?:^|/)[\w.-]*values[\w.-]*\.ya?ml$"
    r"|(?:^|/)(?:application|bootstrap)[\w.-]*\.(?:ya?ml|properties)$"
    r"|\.properties$|\.env$", re.I)
_TEST_PATH = re.compile(r"(?:^|/)test/|(?:^|/)tests?/|Tests?\.(?:java|kt)$|IT\.(?:java|kt)$"
                        r"|(?:^|/)test_[\w]+\.py$|_test\.(?:py|go)$|\.(?:spec|test)\.[jt]sx?$", re.I)
#: A file whose mistakes are expensive however small the diff — by where it
#: lives, not by a word in a sentence: a durable workflow, money, who may do what.
_RISKY_PATH = re.compile(r"workflow|billing|invoic|pricing|price|payment|auth|security|"
                         r"permission|credential", re.I)
#: A file other code depends on as a contract.
_CONTRACT_FILE = re.compile(r"\.avsc$|\.proto$|(?:^|/)schemas?/|migration|liquibase|flyway|"
                            r"changelog|openapi|swagger|\.graphql$", re.I)
#: A class or method named in STRUCTURE without a file ("WidgetMapper.apply()").
_CODE_NAME = re.compile(r"^[\W\d_]*([A-Z][a-z0-9]+[A-Z]\w*)(?:\.\w+\(?\)?)?\b")
#: A STRUCTURE line that names something the change does NOT touch.
_UNTOUCHED = re.compile(r"\b(?:untouched|unchanged|no change|read[- ]only)\b|⚪", re.I)

#: His ask is a config value: "enable XX as S4 country", "update helm", "add X to the list".
_CONFIG_ASK = re.compile(
    r"\b(?:helm|values?\.ya?ml|prod[- ]?values|env(?:ironment)?\s+var\w*|config(?:uration)?\s+value"
    r"|properties|application\.ya?ml|feature\s+flag|toggle|"
    r"as\s+(?:an?\s+)?s4\s+country|country\s+list|add\s+\w+\s+(?:to|in)\s+(?:the\s+)?list)\b", re.I)
#: His ask names code to change.
_CODE_ASK = re.compile(r"\b(?:class|method|mapper|validator|service\s+logic|logic|refactor|"
                       r"implement\w*|null\s*check|npe|exception|endpoint|consumer|producer|"
                       r"workflow|activity|test\s+case|unit\s+test)\b", re.I)
#: His ask is a fix.
_BUG = re.compile(r"\b(?:fix\w*|bug|error|npe|exception|fail(?:s|ing|ed|ure)?|broken|crash\w*|"
                  r"issue|not\s+working)\b", re.I)
#: The cause is already known: a file or line he names, a reviewer's comment,
#: or an earlier investigation handed to the task.
_CAUSE_KNOWN = re.compile(r"\b[\w/.-]+\.(?:java|kt|py|ts|go|ya?ml|xml)(?::\d+)?\b|\bline\s+\d+\b"
                          r"|\breview(?:er)?\s+comments?\b|\[Prior investigation \(task #", re.I)


#: "use claude", "on copilot", "with codex" — which brain he wants the job on.
_BRAIN_OVERRIDE = re.compile(r"\b(?:use|on|with|via)\s+(claude|copilot|codex)\b", re.I)
_CHEAP = re.compile(r"\b(cheap(ly)?|quick and cheap|low effort|don'?t overthink)\b", re.I)
#: Deliberately narrow: "raise max retries to 5" is not a request for T3.
_MAX = re.compile(r"\b(max(imum)? effort|go max|max tier|use (the )?best model|"
                  r"use opus|think hard)\b|[,;—-]\s*max\s*[.!]*$", re.I)


@dataclass(frozen=True)
class Choice:
    tier: int
    model: str          # '' = the brain's own default
    effort: str
    why: str


@dataclass(frozen=True)
class Size:
    tier: int
    why: str
    #: "config" (T1 values only), "big" / "modification" (T3), or "".
    flavor: str = ""


def overrides(text: str) -> dict:
    """What he asked for in so many words: a brain, and/or a tier."""
    t = text or ""
    out: dict = {}
    m = _BRAIN_OVERRIDE.search(t)
    if m:
        out["brain"] = m.group(1).lower()
    if _MAX.search(t):
        out["tier"] = T3
    elif _CHEAP.search(t):
        out["tier"] = T1
    return out


def pre_tier(title: str, prompt: str, his: str = "") -> tuple[int, str]:
    """The tier cheap signals suggest before any brain has looked."""
    size = pre_size(title, prompt, his)
    return size.tier, size.why


def pre_size(title: str, prompt: str, his: str = "") -> Size:
    """The size before a plan exists — from HIS words when the task came from his
    turn; the brief a brain wrote is only background (19 Nov: a one-line ask
    became a long brief, read as "standard", and planned on opus)."""
    full = f"{title}\n{prompt}"
    text = (his or "").strip() or full
    if _PROD_DOWN.search(text):
        return Size(T3, "prod is down", "big")
    if _CONTRACT.search(text):
        return Size(T3, f"touches a contract ({_CONTRACT.search(text).group(0)})", "big")
    files = set(_FILE.findall(text))
    if len(files) > BIG_FEATURE_FILES:
        return Size(T3, f"a big feature ({len(files)} files named)", "big")
    ticket = _TICKET.search(f"{full}\n{his}")
    if ticket or len(files) >= 3:
        return Size(T2, "a ticket" if ticket else f"{len(files)} files named")
    if his.strip():
        if _CONFIG_ASK.search(his) and not _CODE_ASK.search(his):
            return Size(T1, "a config value", "config")
        if _BUG.search(his):
            if _CAUSE_KNOWN.search(f"{his}\n{prompt}"):
                return Size(T1, "a fix whose cause is known")
            return Size(T2, "a fix whose cause is not known yet")
        named = set(_FILE.findall(his))
        if 0 < len(named) <= 2:
            return Size(T1, f"{len(named)} file(s) named")
        if _SMALL.search(his):
            return Size(T1, "a small, single-place change")
        return Size(T2, "standard")
    if _SMALL.search(text) or len(text) < 160:
        return Size(T1, "a small, single-place change")
    return Size(T2, "standard")


def _structure_entries(plan: str) -> int:
    """How many classes/files the plan's STRUCTURE block lists (its sub-lines —
    `└─ Service.method()` — belong to the entry above them)."""
    from .tasks import _STRUCTURE_HEAD, _block_span
    lines = (plan or "").splitlines()
    start, end = _block_span(lines, _STRUCTURE_HEAD)
    if start < 0:
        return 0
    return sum(1 for ln in lines[start + 1:end]
               if ln.strip() and not ln.strip().startswith(("└", "├", "│")))


def _structure_lines(plan: str) -> list[str]:
    from .tasks import _STRUCTURE_HEAD, _block_span
    lines = (plan or "").splitlines()
    start, end = _block_span(lines, _STRUCTURE_HEAD)
    return lines[start + 1:end] if start >= 0 else []


def kind_of(path: str) -> str:
    """config | test | code, for one file path."""
    if _TEST_PATH.search(path):
        return "test"
    if _CONFIG_PATH.search(path):
        return "config"
    return "code"


def touched(plan: str) -> dict[str, set[str]]:
    """The files (or classes) the plan says it changes, by kind. Only the
    STRUCTURE block counts when there is one: prose like "no schema change" or
    "the mapper reads it, unchanged" names things the change does not touch."""
    lines = [ln for ln in _structure_lines(plan) if ln.strip() and not _UNTOUCHED.search(ln)]
    if not lines:
        lines = [ln for ln in (plan or "").splitlines() if not _UNTOUCHED.search(ln)]
    out: dict[str, set[str]] = {"config": set(), "test": set(), "code": set()}
    for ln in lines:
        found = _FILE.findall(ln)
        for f in found:
            if not f.endswith(".md"):
                out[kind_of(f)].add(f)
        if not found:
            m = _CODE_NAME.match(ln.strip().lstrip("✏️🆕🧪└├│- *`"))
            if m:
                out["test" if m.group(1).endswith("Test") else "code"].add(m.group(1))
    return out


def plan_size(plan: str, outcome: dict | None = None) -> Size:
    """The size the plan calls for — from the files it changes, not its words."""
    outcome = outcome or {}
    repos = [r for r in outcome.get("repos") or [] if r]
    files = touched(plan)
    code, tests, cfg = files["code"], files["test"], files["config"]
    structure = "\n".join(_structure_lines(plan)) or (plan or "")
    if cfg and not code and not tests:
        where = f", {len(repos)} repos" if len(repos) > 1 else ""
        return Size(T1, f"config only ({len(cfg)} file(s){where})", "config")
    contract = next((f for f in code if _CONTRACT_FILE.search(f)), "") \
        or (_CONTRACT.search(structure).group(0) if _CONTRACT.search(structure) else "")
    if contract:
        return Size(T3, f"changes a contract ({contract})", "big")
    if len(code) > BIG_FEATURE_FILES:
        return Size(T3, f"a big feature ({len(code)} code files)", "big")
    if len(repos) >= 2 and code:
        return Size(T3, f"code in {len(repos)} repos", "big")
    risky = next((f for f in sorted(code) if _RISKY_PATH.search(f)), "")
    if not risky and code and _IMPACT.search(structure):
        risky = _IMPACT.search(structure).group(0)
    if risky:
        return Size(T3, f"a change on a risky path ({risky})", "modification")
    if re.search(r"\brisk\s*[:=]\s*high\b", plan or "", re.I):
        return Size(T3, "the plan rates its risk high", "modification")
    # Once code changes, the config beside it is part of the same change.
    n = len(code) + len(tests) + (len(cfg) if code else 0)
    if not n:
        n = _structure_entries(plan)
    if n > BIG_FEATURE_FILES:
        return Size(T3, f"a big feature ({n} classes/files)", "big")
    if n >= 3:
        return Size(T2, f"{n} classes/files")
    if n:
        return Size(T1, f"{n} class(es)/file(s), one repo")
    return Size(T2, "size not stated")


def plan_tier(plan: str, outcome: dict | None = None) -> tuple[int, str]:
    """The tier the approved plan calls for — the true size of the change."""
    size = plan_size(plan, outcome)
    return size.tier, size.why


def diff_size(counts: dict[str, int], lines: int) -> Size:
    """The size of the change that was actually made (`fit`)."""
    code, tests, cfg = counts.get("code", 0), counts.get("test", 0), counts.get("config", 0)
    if cfg and not code and not tests:
        return Size(T1, f"config only ({cfg} file(s))", "config")
    n = code + tests + (cfg if code else 0)
    if code > BIG_FEATURE_FILES or lines > 1500:
        return Size(T3, f"{n} files, {lines} lines", "big")
    if n >= 3 or lines > 200:
        return Size(T2, f"{n} files, {lines} lines")
    return Size(T1, f"{n} file(s), {lines} lines")


# --- what each tier means on each brain ------------------------------------------------

#: (model, effort) per brain per tier and stage. Claude CLI takes model aliases
#: (haiku / sonnet / opus resolve to the current models); Copilot's model list
#: differs by account, so it keeps its own and moves on effort. Override any cell
#: with ASTA_ROUTE_<BRAIN>_<T#>="model:effort".
_TABLE = {
    # His call, 19 Nov: small work builds on haiku; everything else builds on
    # sonnet at high — the plan carries the hard thinking.
    "claude": {T1: ("haiku", "medium"), T2: ("sonnet", "high"), T3: ("sonnet", "high")},
    "copilot": {T1: ("", "low"), T2: ("", "high"), T3: ("", "xhigh")},
    # pi builds on the same Claude models (pi_cli.model_id maps the aliases). Its
    # "haiku" is an older model without effort levels, so T1 stays on sonnet.
    "pi": {T1: ("sonnet", "medium"), T2: ("sonnet", "high"), T3: ("sonnet", "high")},
}
#: The plan leg. Override with ASTA_ROUTE_<BRAIN>_PLAN_<T#>.
_PLAN = {
    "claude": {T1: ("haiku", "medium"), T2: ("sonnet", "high"), T3: ("opus", "high")},
    "copilot": {T1: ("", "medium"), T2: ("", "high"), T3: ("", "xhigh")},
}
#: Within a tier, the kind of work: a config value needs little thinking, and a
#: small change on a risky path plans at medium — a big feature at high.
_FLAVOR = {
    ("claude", "plan", T1, "config"): ("haiku", "low"),
    ("claude", "build", T1, "config"): ("haiku", "low"),
    ("claude", "plan", T3, "modification"): ("opus", "medium"),
}
#: Debugging a booking: tracing evidence across services is multi-step work —
#: never haiku, never "low". A second pass on the same case thinks harder.
DEBUG = ("sonnet", "medium")
DEBUG_AGAIN = ("sonnet", "high")
#: A chat turn that only reads what Asta holds (chat_light).
_LIGHT = ("sonnet", "low")


def _cell(brain: str, tier: int, stage: str = "", flavor: str = "") -> tuple[str, str]:
    table = _PLAN if stage == "plan" else _TABLE
    key = f"ASTA_ROUTE_{brain.upper()}_{'PLAN_' if stage == 'plan' else ''}{NAMES[tier]}"
    raw = os.environ.get(key, "").strip()
    if raw:
        model, _, effort = raw.partition(":")
        return model.strip(), effort.strip()
    cell = _FLAVOR.get((brain, "plan" if stage == "plan" else "build", tier, flavor))
    return cell or table.get(brain, {}).get(tier, ("", ""))


# --- chat turns ----------------------------------------------------------------------
# 12 Nov: a chat turn cost ~$0.33 — ~80k tokens of resumed session re-read for
# every message, "who messaged me" included. A read of what Asta already holds
# needs neither the long session nor the deep model: it runs at T1, in a one-off
# session with the last few lines of the conversation. Deep work keeps the full one.

#: Questions that READ state: messages, PR/task status, a task's result.
_LIGHT_READ = re.compile(
    r"^\W*(?:(?:hey\s+|hi\s+|ok\s+)?asta\W+)?(?:"
    r"who\s+(?:all\s+)?(?:messaged|pinged|mailed|called|wrote\s+to|tagged|mentioned)\s+me"
    r"|(?:any(?:thing)?|new)\s+(?:new\s+)?(?:messages?|pings?|mails?|emails?)\b"
    r"|what(?:'s|\s+is|\s+are)\s+(?:the\s+)?status\s+of\s+(?:my\s+|the\s+)?(?:open\s+)?"
    r"(?:prs?|pull\s+requests?|tasks?)\b"
    r"|status\s+of\s+(?:my\s+|the\s+)?(?:open\s+)?(?:prs?|pull\s+requests?|tasks?)\b"
    r"|(?:my\s+)?open\s+prs?\b"
    r"|task\s*#?\s*\d{1,5}\s+(?:result|status|update|findings?)\b"
    r"|what\s+did\s+task\s*#?\s*\d{1,5}\s+(?:find|conclude|say)\b"
    r"|what(?:'s|\s+is)\s+(?:still\s+)?pending\b)", re.I)
#: An order anywhere in it makes it work, not a read: "…and send it to Priya".
_ORDER = re.compile(
    r"(?:^|[.;!?]\s*|\b(?:and|then|also|pls|please)\s+)"
    r"(?:fix|send|post|approve|merge|raise|implement|draft|reply|tell|ping|review|open|create|"
    r"delete|close|run|deploy|change|update|add|remove|rerun|retry|cancel|stop|investigate|"
    r"debug|check|ask|share|forward|comment|push|commit)\b", re.I)


def chat_light(text: str) -> bool:
    """A short read of what Asta holds, with no order in it."""
    t = (text or "").strip()
    return 0 < len(t) <= 200 and bool(_LIGHT_READ.search(t)) and not _ORDER.search(t)


def chat_choice(brain: str, text: str) -> Choice | None:
    """T1 for a light read on Claude; None = the full session at his chosen model."""
    if not brain.startswith("claude") or not chat_light(text):
        return None
    model, effort = _LIGHT
    return Choice(T1, model, effort, "a read of what Asta already holds")


def _key(task_id: int) -> str:
    return f"task_tier:{task_id}"


def _saved(task_id: int) -> dict:
    try:
        d = json.loads(store.kv_get(_key(task_id)) or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def tier_of(task_id: int) -> int:
    try:
        return int(_saved(task_id).get("tier") or 0)
    except (ValueError, TypeError):
        return 0


def flavor_of(task_id: int) -> str:
    return str(_saved(task_id).get("flavor") or "")


def set_tier(task_id: int, tier: int, why: str, source: str, flavor: str = "") -> None:
    tier = max(T1, min(T3, tier))
    store.kv_set(_key(task_id), json.dumps({"tier": tier, "why": why, "source": source,
                                            "flavor": flavor}))
    store.add_task_event(task_id, "tier", f"{NAMES[tier]} — {why} ({source})")


def on_spawn(task_id: int, title: str, prompt: str) -> str:
    """Record the starting tier, and honour a brain he named. Returns the brain
    he asked for, or ''."""
    ov = overrides(f"{title}\n{prompt}")
    if "tier" in ov:
        set_tier(task_id, ov["tier"], "you asked for it", "override")
        store.kv_set(f"task_tier_pinned:{task_id}", "1")
    else:
        size = pre_size(title, prompt, _his_words())
        set_tier(task_id, size.tier, size.why, "signals", size.flavor)
    return ov.get("brain", "")


def _his_words() -> str:
    """What he typed this turn ("" outside his turn), never the brief."""
    try:
        from . import capabilities, scorecard
        return scorecard.his_words(capabilities.said_this_turn())
    except Exception:                                       # noqa: BLE001
        return ""


def on_plan(task_id: int, plan: str, outcome: dict | None = None) -> None:
    """Re-tier from the approved plan — unless he pinned a tier himself."""
    if store.kv_get(f"task_tier_pinned:{task_id}"):
        return
    size = plan_size(plan, outcome)
    set_tier(task_id, size.tier, size.why, "plan", size.flavor)


def fit(task_id: int, counts: dict[str, int], lines: int) -> str:
    """The real diff against the tier the build ran at: "hit", "under" or "over".

    Under: the remaining legs (verify fixes, review rounds) move up to the size of
    the work — 272 and 419 were built at T1 while being 9–17 code files. Counted
    once per task on the scorecard either way; a tier he pinned is not moved."""
    if store.kv_get(f"task_route_fit:{task_id}"):
        return store.kv_get(f"task_route_fit:{task_id}") or ""
    tier = tier_of(task_id) or T2
    real = diff_size(counts, lines)
    verdict = "under" if real.tier > tier else "over" if real.tier < tier else "hit"
    store.kv_set(f"task_route_fit:{task_id}", verdict)
    store.record_outcome("route_fit", verdict, subject=str(task_id),
                         detail=f"ran {NAMES[tier]}, change was {NAMES[real.tier]}: {real.why}")
    if verdict == "under" and not store.kv_get(f"task_tier_pinned:{task_id}"):
        set_tier(task_id, real.tier, f"the change is bigger than planned ({real.why})", "fit",
                 real.flavor)
    return verdict


def escalate(task_id: int, why: str) -> int:
    """The same failure twice: one tier up, once."""
    tier = min(T3, (tier_of(task_id) or T2) + 1)
    set_tier(task_id, tier, why, "escalation")
    return tier


def choose(task_id: int, brain: str, stage: str) -> Choice:
    """The model and effort for this leg, on the brain the task is on."""
    brain = "claude" if brain.startswith("claude") else brain
    flavor = flavor_of(task_id)
    if stage == "plan":
        tier = tier_of(task_id) or T2
        model, effort = _cell(brain, tier, "plan", flavor)
        pinned = store.kv_get(f"task_tier_pinned:{task_id}") and tier == T3
        return Choice(tier, model, effort,
                      "planning at T3 — you asked for max" if pinned else
                      f"plan at {NAMES[tier]} — the best model plans anything ticket-sized")
    tier = tier_of(task_id) or T2
    model, effort = _cell(brain, tier, "", flavor)
    return Choice(tier, model, effort, f"{NAMES[tier]}{' ' + flavor if flavor else ''} on {brain}")


def debug_choice(brain: str, prompt: str) -> Choice | None:
    """Model and effort for a debugging task (a booking case, an incident) — None
    for any other analysis, which keeps the brain's own settings."""
    from . import roles
    text = prompt or ""
    briefed = any(f"you are {roles.ROLES[r][0]}" in text for r in ("debugging", "infra")
                  if r in roles.ROLES)
    if not briefed and roles.role_for(text) not in ("debugging", "infra"):
        return None
    again = bool(re.search(r"\[Prior investigation \(task #|\bNot confirmed\b", prompt or "")) \
        or bool(_PROD_DOWN.search(prompt or ""))
    model, effort = DEBUG_AGAIN if again else DEBUG
    if not brain.startswith("claude"):
        model = ""                       # Copilot keeps its own model; effort still applies
    return Choice(T2, model, effort, "debugging — a second pass" if again else "debugging")


def record(task_id: int, stage: str, brain: str, c: Choice) -> None:
    detail = f"{stage}: {brain} {c.model or 'default'} @ {c.effort or 'default'} — {c.why}"
    store.add_task_event(task_id, "route", detail)
    store.record_outcome("route", NAMES[c.tier], subject=str(task_id), detail=detail)
