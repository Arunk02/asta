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
    address it to a different Vinish, decide the tool call was optional, or answer
    about the send instead of performing it. All four look identical to Arun,
    because all four end with the message not arriving.
    """
    from . import teams_bridge
    where = await teams_bridge.send_message(to, text, allow_group=to_group)
    return f"✅ Sent to {where}."


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


async def run(op_spec: dict) -> str:
    """Execute a staged operation and return the line Arun reads.

    An unknown name is refused rather than ignored: a silent no-op after a yes is
    the worst of both — he believes it is done and nobody says otherwise.
    """
    entry = REGISTRY.get(op_spec.get("name", ""))
    if entry is None:
        raise RuntimeError(f"unknown operation '{op_spec.get('name', '?')}' — "
                           f"nothing was done")
    return await entry["run"](**(op_spec.get("args") or {}))
