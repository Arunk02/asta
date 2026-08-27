"""What a scenario actually executes — the real function, never a stand-in.

The whole harness is worthless if these call mocks. A bench that exercises a
double proves the double works; the thing Arun uses is the code in `triage`,
`writing`, `tasks` and `token_audit`, so that is what runs here. Where a
capability genuinely needs a brain, the runner spends the tokens and the case is
marked `live` so nobody pays for it by accident.

Each runner takes a case and returns an observation:

    {"text": str, "tokens": int, "violations": list[str]}

`text` is what gets graded, so a runner's real job is rendering a decision into
something a `must` can match: not the Verdict object, but "action=True asks you
directly Vinish: …". That keeps the scenarios readable as data and keeps the
grading in one place (`evals.grade`) instead of one comparison per capability.

`violations` is for rules that must hold regardless of quality — a draft that
still carries a term of address Arun has never used for that person, a chat turn
that wrote a file. Those cap the reward in `bench.score` rather than reducing it,
because being fast is not a defence.
"""

from __future__ import annotations

from typing import Awaitable, Callable


def _given(case: dict) -> dict:
    return case.get("given", {}) or {}


# --- triage: does this need him, and how urgently ----------------------------

async def _triage(case: dict) -> dict:
    """`triage.classify` — pure, free, and the most-exercised decision in Asta."""
    from . import triage
    g = _given(case)
    v = triage.classify(g.get("who", ""), g.get("subject", ""),
                        g.get("preview", ""), addressed=g.get("addressed"))
    # Rendered so a case can assert on the DECISION ("action=True") as easily as
    # on the wording. Both matter: the decision drives whether his phone buzzes,
    # the wording is what he then reads.
    return {"text": f"action={v.action} why={v.why} line={v.one_line}"}


# --- summarise: compressing a pile into the part worth reading ---------------

async def _summarise(case: dict) -> dict:
    """`triage.summarize` over verdicts built from the case's items."""
    from . import triage
    verdicts = []
    for item in _given(case).get("items", []):
        v = triage.classify(item.get("who", ""), item.get("subject", ""),
                            item.get("preview", ""), addressed=item.get("addressed"))
        if item.get("priority") is not None:
            v = v.ranked(int(item["priority"]))
        verdicts.append(v)
    text, needs = triage.summarize(verdicts, _given(case).get("source", "💬 Teams"))
    return {"text": f"needs={needs}\n{text}"}


# --- analyse: reading a failure and naming its cause -------------------------

async def _analyse(case: dict) -> dict:
    """`token_audit.detect_waste` — given a session's shape, name what it wasted.

    Analysis with a right answer: the categories are facts about the record, so
    this is gradeable without asking a model for a second opinion.
    """
    from . import token_audit
    waste = token_audit.detect_waste(_given(case).get("record", {}))
    hits = sorted(k for k, v in waste.items() if v)
    return {"text": "waste=" + (",".join(hits) if hits else "none")}


# --- message: writing to a colleague in his voice ----------------------------

async def _message(case: dict) -> dict:
    """`writing.fit_address` on a draft, then structural checks against his style.

    The guardrail is what is being measured. A model told the rule still slips,
    and the cost is a message calling a colleague something Arun never has — a
    thing he would have to apologise for rather than correct.
    """
    from . import writing
    g = _given(case)
    draft = g.get("draft", "")
    chat = g.get("chat", "")
    fitted = writing.tidy_links(writing.fit_address(draft, chat))
    violations = []
    for banned in g.get("must_not_address", []) or []:
        # Whole-word, case-insensitive: "bro" must not survive, "brochure" may.
        import re
        if re.search(rf"\b{re.escape(banned)}\b", fitted, re.I):
            # A genuine violation: this one goes OUT, to a person, in his name.
            violations.append(f"still addresses them as '{banned}'")
    cap = int(g.get("max_chars") or 0)
    too_long = bool(cap and len(fitted) > cap)
    # Length is graded, not a violation. The line is deliberate: a violation is
    # something that would embarrass him if it went out; everything else the
    # cases grade for themselves. Conflating the two caps the reward on cases
    # whose whole purpose is to OBSERVE a bad draft.
    return {"text": f"too_long={too_long} chars={len(fitted)}\n{fitted}",
            "violations": violations}


# --- plan: something he can grasp in thirty seconds --------------------------

async def _plan(case: dict) -> dict:
    """The plan gate's own rules, applied to a plan: structure, brevity, files.

    Uses `tasks._structure_span` rather than a fresh regex so a change to the
    gate's wording moves this measurement with it instead of leaving it stale.
    """
    from . import tasks
    g = _given(case)
    plan = g.get("plan", "")
    lines = plan.splitlines()
    start, end = tasks._structure_span(lines)
    has_structure = start >= 0
    words = len(plan.split())
    # Thirty seconds standing up, reading a phone, is about 130 words. Not a
    # number he has to take on trust — it is what the gate already promises him,
    # made checkable. Graded, never a violation: a case that exists to prove a
    # bad plan is caught must be able to observe one without being punished.
    too_long = words > int(g.get("max_words") or 130)
    return {
        "text": (f"structure={has_structure} too_long={too_long} "
                 f"words={words} lines={len(lines)}\n{plan}"),
    }


# --- recover: healing itself without him -------------------------------------

async def _recover(case: dict) -> dict:
    """Drive `recovery.ladder` against a simulated fault and report what it did.

    The capability Arun actually asked for: not "did it notice", which Asta has
    always been good at, but "did it fix the thing without me".
    """
    from . import recovery
    g = _given(case)
    trace: list[str] = []

    heals_at = g.get("heals_at", "")
    throws = set(g.get("throws", []) or [])

    def make(name: str) -> Callable[[], Awaitable[bool]]:
        async def go() -> bool:
            trace.append(name)
            if name in throws:
                # A repair step can itself be too broken to run — the ladder has
                # to survive that, because the blunter rung below is exactly what
                # such a state needs.
                raise RuntimeError(f"{name} could not run")
            return name == heals_at
        return go

    rungs = [(n, make(n)) for n in g.get("rungs", [])]
    outcome = await recovery.ladder(
        g.get("source", "teams"), rungs,
        stale_polls=int(g.get("stale_polls", 99)),
        threshold=int(g.get("threshold", 3)))
    return {"text": f"healed={outcome['healed']} by={outcome['healed_by']} "
                    f"told_him={outcome['told_him']} tried={','.join(trace)}"}


# --- code: is this work, whose ticket, and is it worth a brain at all --------

async def _code(case: dict) -> dict:
    """The routing decisions that start every code task — all pure, all free.

    This is where his two most expensive failures begin. Routing a QUESTION to a
    code task spawns work he never asked for; routing a real assignment to chat
    means a plan gate that never runs. And `is_trivial` is the token-waste
    decision in its purest form: "ok" must never reach a paid model.
    """
    from . import router, work_intent
    g = _given(case)
    text = g.get("text", "")
    repos = tuple(g.get("repos", []) or [])
    return {"text": (
        f"work={work_intent.is_work_assignment(text, repos)} "
        f"trivial={router.is_trivial(text)} "
        f"ticket={work_intent.ticket_in(text) or 'none'} "
        f"title={work_intent.title_for(text)}")}


# --- jira: reading a ticket and knowing what it actually asks for ------------

async def _jira(case: dict) -> dict:
    """Parse a ticket fixture the way the real reader does — no network.

    Ticket bodies here are FIXTURES, not live issues: a bench that hits Jira
    measures Jira's uptime as much as Asta's, and would put read load on a
    corporate system every time someone runs the suite.
    """
    from . import jira
    g = _given(case)
    issue = g.get("issue", {})
    parts = [f"key={issue.get('key', '')}", f"status={issue.get('status', '')}"]
    # Acceptance criteria hide in comments — one of the lessons that cost him a
    # round-trip on a real ticket, and the reason comments are read at all.
    body = " ".join([issue.get("summary", ""), issue.get("description", "")]
                    + [c.get("body", "") for c in issue.get("comments", [])])
    parts.append(f"sprint_jql={jira.sprint_jql()}" if g.get("want_jql") else "")
    return {"text": " ".join(p for p in parts if p) + "\n" + body}


# --- context: using what the workspace already knows -------------------------

async def _context(case: dict) -> dict:
    """The existing grounded Q&A suite, reached through its own entry point.

    `evals` stays the owner of workspace answer quality — this only borrows it so
    one capability's score sits on the same scoreboard as the other eight.
    """
    from . import meetings
    g = _given(case)
    answer = await meetings.answer_from_knowledge(case.get("ask", ""), g.get("playbook", ""))
    return {"text": answer or ""}


#: capability -> runner. A capability with no runner scores as an error rather
#: than silently as a zero, so a typo in a suite is visible instead of damning.
RUNNERS: dict[str, Callable[[dict], Awaitable[dict]]] = {
    "triage": _triage,
    "summarise": _summarise,
    "analyse": _analyse,
    "message": _message,
    "plan": _plan,
    "code": _code,
    "jira": _jira,
    "recover": _recover,
    "context": _context,
}


def for_capability(name: str):
    return RUNNERS.get(name)
