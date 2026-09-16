"""The follow-through graph: a promise that outlives the turn it was made in.

    watch ──► judge ──► nudge ──► watch
                │  │
                │  └─► warn ──► gate ⏸ ──► watch
                └─► close

*"track on with alex and get this all these 3 PR's for hot priority, get review
and merged by tmr EOD, if not done let me know what issue notify me"* — a
promise, not a question and not a code task. `followup` already keeps the
commitment and does the chasing; what it has no way to do is PAUSE.

That matters at exactly one point, and it is the point that matters most: the
warning before the deadline. "Let me know if it isn't done" said after EOD is a
report; said four hours before, it is still something he can act on — and what
he says back ("chase them", "leave it", "I'll do it myself") has to be waited
for, across a restart, without the chase carrying on underneath it. A daemon
loop cannot wait; a checkpointed thread can.

  THE NUDGE IS STAGED, NEVER SENT. Unchanged from `followup`, and the reason is
  his standing rule: nothing leaves the machine without his yes. A tracker that
  messages his colleagues at 3am is the thing that rule exists to prevent.

  A CLOSED PROMISE IS RECORDED. The outcome says what actually happened —
  merged, abandoned, overtaken — rather than the thread simply going quiet, so
  "what did you do about those PRs" is answerable from state.
"""

from __future__ import annotations

import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class Promise(TypedDict, total=False):
    #: The follow-up row this thread is about. Declared, because LangGraph keeps
    #: ONLY the keys a state schema names: an undeclared `id` is dropped between
    #: the caller and the first node, and the watcher then looks up a promise
    #: that never arrived and reports it "gone" — which reads exactly like a
    #: promise that was kept.
    id: int
    goal: str
    person: str
    urls: list
    due_at: float
    state: str           # what the watcher last saw
    moved: bool          # did anything change since the last look
    nudges: int
    warned: bool
    answer: dict         # what he said when warned
    outcome: str         # kept | dropped | his_call


#: Injected: WATCHER(promise) -> {"state": …, "moved": bool, "done": bool};
#: NUDGER(promise) stages a message (never sends); WARNER(promise) tells him.
WATCHER = None
NUDGER = None
WARNER = None

#: How close to the deadline counts as "before it lands", in seconds.
WARN_BEFORE = 4 * 3600


async def _call(fn, state, default=None):
    if fn is None:
        return default
    out = fn(dict(state))
    if hasattr(out, "__await__"):
        out = await out
    return out


async def watch(state: Promise) -> dict:
    seen = await _call(WATCHER, state, default={}) or {}
    return {"state": seen.get("state", state.get("state", "")),
            "moved": bool(seen.get("moved")),
            "outcome": "kept" if seen.get("done") else ""}


def judge(state: Promise) -> str:
    if state.get("outcome") == "kept":
        return "close"
    due = float(state.get("due_at") or 0)
    if due and not state.get("warned") and time.time() >= due - WARN_BEFORE:
        return "warn"
    return "watch" if state.get("moved") else "nudge"


async def nudge(state: Promise) -> dict:
    """Staged for his yes. This node never reaches the person."""
    await _call(NUDGER, state)
    return {"nudges": int(state.get("nudges") or 0) + 1}


async def warn(state: Promise) -> dict:
    """Before the deadline, while it is still actionable."""
    await _call(WARNER, state)
    return {"warned": True}


def gate(state: Promise) -> dict:
    return {"answer": interrupt({"gate": "deadline",
                                 "goal": state.get("goal", ""),
                                 "state": state.get("state", "")})}


def after_gate(state: Promise) -> str:
    said = (state.get("answer") or {}).get("text", "").strip().lower()
    if said.startswith(("leave", "drop", "stop", "no")):
        return "close"
    return "watch"


async def close(state: Promise) -> dict:
    said = (state.get("answer") or {}).get("text", "").strip().lower()
    if state.get("outcome") == "kept":
        return {"outcome": "kept"}
    return {"outcome": "his_call" if said else "dropped"}


def build() -> StateGraph:
    g = StateGraph(Promise)
    g.add_node("watch", watch)
    g.add_node("nudge", nudge)
    g.add_node("warn", warn)
    g.add_node("gate", gate)
    g.add_node("close", close)
    g.add_edge(START, "watch")
    g.add_conditional_edges("watch", judge,
                            {"watch": END, "nudge": "nudge",
                             "warn": "warn", "close": "close"})
    # A nudge ends the run: the next look is the next tick, not a spin.
    g.add_edge("nudge", END)
    g.add_edge("warn", "gate")
    g.add_conditional_edges("gate", after_gate, {"watch": END, "close": "close"})
    g.add_edge("close", END)
    return g
