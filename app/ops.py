"""The outward acts, and the one place they are allowed to happen.

Everything here changes something a colleague can see: a Jira comment, a status
transition, a review on someone's PR. They share one property that makes them
worth isolating — once done, they cannot be quietly undone, because someone has
already been notified.

So they are not tools a model may call. A model can only PROPOSE one, by staging
it (offers.staged_write), and the proposal records the exact arguments. When Arun
says yes, `run` executes that recorded call. Nothing in between re-reads the
instruction and decides what it meant.

That distinction sounds pedantic until you watch it matter. "Comment on PROJ-412
that the migration is blocked on the schema review" run through a brain a second
time is a different sentence every time, and the sentence he approved is not
necessarily the one that gets posted. Here, it is.

Adding an op means adding one entry to REGISTRY. The signature is uniform —
async, keyword args from the offer, returns the line Arun reads — so the dispatch
side never grows a branch per operation.
"""

from __future__ import annotations

from . import jira, meetings, review

#: op name -> (callable, human description). The description is what he sees in
#: the offer, so it must name the target and paraphrase nothing about intent.
REGISTRY: dict[str, dict] = {}


def op(name: str, describe):
    def register(fn):
        REGISTRY[name] = {"run": fn, "describe": describe}
        return fn
    return register


@op("teams_send", lambda a: (f"Message {a.get('to', '?')} on Teams"
                             + (" (GROUP)" if a.get("to_group") else "")))
async def _teams_send(to: str = "", text: str = "", to_group: bool = False) -> str:
    """Send the approved words, unchanged.

    This is the op that was missing. A Teams send used to be approved and then
    handed BACK to a brain as a prompt saying "send this now" — which is the one
    thing the module docstring above says never to do. Everything that made a
    staged Jira comment trustworthy was absent here: the brain could reword it,
    address it to a different Alex, decide the tool call was optional, or answer
    about the send instead of performing it. All four look identical to Arun,
    because all four end with the message not arriving.
    """
    from . import attention, teams_bridge
    where = await teams_bridge.send_message(to, text, allow_group=to_group)
    # A reply IS the answer. Without this, Asta sent the message he approved and
    # then went on chasing him at end of day about the very question it had just
    # answered on his behalf — "Retry fix merged?" was still listed as waiting on him
    # after the reply had gone out. Settled under the name Teams actually opened,
    # which is the one the ledger stored, not the shorter one he typed.
    settled = attention.settle_with(where) or attention.settle_with(to)
    tail = f" ({settled} cleared from your list)" if settled else ""
    return f"✅ Sent to {where}.{tail}"


@op("teams_call", lambda a: f"Call {a.get('who', '?')} on Teams")
async def _teams_call(who: str = "", video: bool = False) -> str:
    import asyncio as _asyncio
    result = await meetings.call_person(who, video=video)
    # The join paths have always spawned a watcher; this one never did. Without
    # it a placed call is never noticed ringing out, never hung up, never
    # reported — and `teams_in_call` stays set, so the NEXT call is refused as
    # "already in a call" until the server restarts.
    _asyncio.create_task(meetings.call_watch(result))
    return f"📞 Calling {result} — I'll tell you if they pick up, and how it went."


@op("meeting_join", lambda a: f"Join {a.get('title') or 'the meeting'}"
                              + (" and take part" if a.get("speak") else " (listen only)"))
async def _meeting_join(title: str = "", join_url: str = "", speak: bool = False) -> str:
    """Join the meeting he was offered. Silent unless he asked for the other thing.

    Prefers the URL the offer captured over re-resolving the title: between the
    ping and his yes the calendar can move on, and joining the WRONG call puts him
    in a room in front of people who watch him arrive.
    """
    import asyncio as _asyncio
    if join_url:
        result = await meetings.join(join_url, speak=speak)
    else:
        result = await meetings.join_by_phrase(title, speak=speak)
    _asyncio.create_task(meetings.watch_and_report(title))
    mode = "taking part" if speak else "listening only"
    return f"📅 {result} — {title or 'the meeting'} ({mode}). I'll report what was said."


@op("call_answer", lambda a: "Answer the call"
                              + (" and talk" if a.get("speak") else " (listen only)"))
async def _call_answer(speak: bool = False) -> str:
    """Pick up the call that is ringing NOW.

    Re-checks the ring rather than trusting the offer. A ring lasts about thirty
    seconds and his yes can arrive after it stopped; clicking Accept on a toast
    that is gone would either do nothing or hit whatever replaced it, and telling
    him "answered" when nothing was answered is the failure that matters.
    """
    from . import incoming, teams_bridge
    async with teams_bridge.teams_page() as page:
        call = await incoming.look(page)
        if not call:
            incoming.clear()
            return "📞 Too late — it stopped ringing before you answered."
        result = await incoming.answer(page, speak=speak)
    return f"📞 {incoming.describe(call)} {result}"


@op("jira_comment", lambda a: f"Comment on {a.get('key', '?')}")
async def _jira_comment(key: str = "", text: str = "") -> str:
    await jira.add_comment(key, text)
    return f"💬 Commented on {key}."


@op("jira_transition", lambda a: f"Move {a.get('key', '?')} → {a.get('to_status', '?')}")
async def _jira_transition(key: str = "", to_status: str = "") -> str:
    result = await jira.transition_issue(key, to_status)
    return f"✅ {key} is now {result['status']}."


@op("pr_review", lambda a: f"{a.get('action', 'comment').replace('_', ' ').title()} "
                           f"PR #{str(a.get('pr', '?')).lstrip('#')}")
async def _pr_review(pr: str = "", workspace: str = "", repo: str = "",
                     action: str = "comment", body: str = "") -> str:
    return "✅ " + await review.post_review(pr, workspace, repo, action, body)


@op("pr_review_inline", lambda a: f"{a.get('action', 'comment').replace('_', ' ').title()} "
                                  f"PR #{str(a.get('pr', '?')).lstrip('#')} with "
                                  f"{len(a.get('comments') or [])} inline comment(s)")
async def _pr_review_inline(pr: str = "", workspace: str = "", repo: str = "",
                            action: str = "comment", body: str = "",
                            comments: list | None = None) -> str:
    return "✅ " + await review.post_inline_review(pr, workspace, repo, action,
                                                  body, comments or [])


@op("pr_merge", lambda a: f"Merge PR #{str(a.get('pr', '?')).lstrip('#')} "
                          f"({a.get('method', 'squash')})")
async def _pr_merge(pr: str = "", workspace: str = "", repo: str = "",
                    method: str = "squash", delete_branch: bool = True) -> str:
    return "🔀 " + await review.merge(pr, workspace, repo, method, delete_branch)


@op("calendar_send", lambda a: f"Send the invite: {a.get('summary', 'calendar invite')}")
async def _calendar_send(url: str = "", summary: str = "") -> str:
    result = await meetings.open_and_send(url, send=True)
    if result != "sent":
        raise RuntimeError(f"invite was {result}")
    return f"📅 Sent — {summary or 'invite'}."


def known(name: str) -> bool:
    return name in REGISTRY


def describe(op_spec: dict) -> str:
    """One line naming what accepting will do — used in the offer he reads."""
    entry = REGISTRY.get(op_spec.get("name", ""))
    if entry is None:
        return f"(unknown operation: {op_spec.get('name', '?')})"
    try:
        return entry["describe"](op_spec.get("args") or {})
    except Exception:
        return op_spec.get("name", "?")


@op("rule_add", lambda a: f"Keep a standing rule: {a.get('words', '')[:80]}")
async def _rule_add(**args) -> str:
    """His yes to a rule the instruction compiler proposed (app/instructions.py)."""
    from . import instructions
    return instructions.adopt(args)


@op("authority_grant", lambda a: (f"Let Asta {a.get('act', 'act')} {a.get('target', '?')} "
                                  f"without asking, up to {a.get('per_day', 1)} a day"))
async def _authority_grant(**args) -> str:
    """His yes to a standing permission (app/authority.py)."""
    from . import authority
    g = authority.grant(args.get("act", ""), args.get("target", ""),
                        int(args.get("per_day", 1) or 1), args.get("words", ""))
    return f"🔓 Permission {g.id}: {g.render()}. Say “revoke {g.id}” to end it."


#: What each outward op DOES, in the words a standing rule is written in, and
#: which argument names its target — so "never message X" stops a Teams send to X.
_ACTS = {"teams_send": ("send", "to"), "teams_call": ("call", "who"),
         "jira_comment": ("comment", "key"), "jira_transition": ("transition", "key"),
         "pr_review": ("review", "pr"), "pr_merge": ("merge", "pr"),
         "calendar_send": ("send", "summary")}


async def run(op_spec: dict) -> str:
    """Execute a staged operation and return the line Arun reads.

    An unknown name is refused rather than ignored: a silent no-op after a yes is
    the worst of both — he believes it is done and nobody says otherwise.

    His standing rules are checked first. His yes to THIS staged act counts as
    asking, so a rule he gave "unless I ask" lets it through; a flat "never" does
    not, and he is told which rule stopped it and how to lift it.
    """
    entry = REGISTRY.get(op_spec.get("name", ""))
    if entry is None:
        raise RuntimeError(f"unknown operation '{op_spec.get('name', '?')}' — "
                           f"nothing was done")
    args = op_spec.get("args") or {}
    act = _ACTS.get(op_spec.get("name", ""))
    if act:
        from . import policy
        d = policy.check(act[0], str(args.get(act[1], "")), asked=True)
        if not d.ok:
            return (f"⛔ Not done — {d.why}. Say “drop rule {d.rule.id}” if that has "
                    f"changed.")
    out = await entry["run"](**args)
    if act:
        # He approved this exact act, as written. Ten of the same and Asta may
        # ask for a standing permission — never sooner, never for a new person.
        from . import authority
        target = str(args.get(act[1], ""))
        authority.note_approved(act[0], target)
        if authority.earned(act[0], target):
            out += "\n\n" + authority.propose(act[0], target)
    return out
