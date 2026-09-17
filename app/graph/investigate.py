"""The investigation graph: go and find out, and survive not finishing in one go.

    read ──► look ──► judge ──► answer
              ▲         │
              └─ again ─┤
                        └─► ask ⏸ ──► look

`responder` already decides that something is worth checking and spawns a
read-only analysis. What it cannot do is last: an investigation is one turn, so a
usage limit halfway through loses everything it had established — the exact
failure that made the deck ask fail twice, one layer up.

Here the findings are STATE. Each look records what it checked and what it found;
a limit, a restart or a crash resumes the thread with those findings intact, and
the next look starts from what is already known rather than from the question.

Two rules carried over from the code graph:

  A GATE NODE ONLY WAITS. `ask` is reached when the question cannot be settled
  without him — one question, not a guess — and the node does nothing but
  `interrupt()`, because a resumed node re-runs from its start.

  ENOUGH IS A DECISION, NOT A FEELING. `judge` stops when the findings answer the
  question or when the checks are exhausted; it never loops forever hoping.
"""

from __future__ import annotations

import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

#: How many looks before it has to say what it has. An investigation that never
#: ends is a brain quietly spending his subscription on a question nobody reads.
MAX_LOOKS = 4


class Look(TypedDict, total=False):
    what: str            # what was checked, in his terms
    found: str           # what came back
    at: float


class Quest(TypedDict, total=False):
    question: str
    who: str             # who asked, so the answer can go back to them
    looks: list          # [Look] — the findings so far, kept across restarts
    verdict: str         # answered | needs_him | exhausted
    answer: str
    asked: dict          # what he said at the gate


#: Filled in by the caller: `check(question, looks) -> Look` does one read-only
#: look and says what it found. Injected rather than imported so the graph can be
#: tested without a brain, and so the same graph serves Teams, mail and chat.
CHECKER = None


async def _check(state: Quest) -> Look:
    if CHECKER is None:
        return {"what": "nothing to check with", "found": "", "at": time.time()}
    out = CHECKER(state["question"], list(state.get("looks") or []))
    if hasattr(out, "__await__"):
        out = await out
    return out


async def look(state: Quest) -> dict:
    found = await _check(state)
    return {"looks": list(state.get("looks") or []) + [dict(found)]}


def judge(state: Quest) -> dict:
    """Enough, not enough, or not knowable without him."""
    looks = list(state.get("looks") or [])
    answered = [lk for lk in looks if (lk.get("found") or "").strip()]
    if answered and (answered[-1].get("found") or "").strip():
        last = answered[-1]
        if str(last.get("found", "")).lower().startswith("ask him:"):
            return {"verdict": "needs_him"}
        return {"verdict": "answered",
                "answer": _render(state.get("question", ""), answered)}
    if len(looks) >= MAX_LOOKS:
        return {"verdict": "exhausted",
                "answer": _render(state.get("question", ""), looks)}
    return {"verdict": ""}


def _render(question: str, looks: list) -> str:
    lines = [f"{lk.get('what', 'checked')}: {lk.get('found', '—')}" for lk in looks]
    return f"{question}\n" + "\n".join(f"• {ln}" for ln in lines)


def after_judge(state: Quest) -> str:
    verdict = state.get("verdict") or ""
    if verdict == "needs_him":
        return "ask"
    if verdict in ("answered", "exhausted"):
        return "answer"
    return "look"


def ask(state: Quest) -> dict:
    """The only gate. One question, and nothing else happens in this node."""
    return {"asked": interrupt({"gate": "investigate",
                                "question": state.get("question", ""),
                                "so_far": list(state.get("looks") or [])})}


def after_ask(state: Quest) -> str:
    said = (state.get("asked") or {}).get("text", "")
    return "look" if said else "answer"


async def answer(state: Quest) -> dict:
    """What it found, with what it checked — evidence, not a verdict on its own."""
    return {"answer": state.get("answer") or _render(
        state.get("question", ""), list(state.get("looks") or []))}


def build() -> StateGraph:
    g = StateGraph(Quest)
    g.add_node("look", look)
    g.add_node("judge", judge)
    g.add_node("ask", ask)
    g.add_node("answer", answer)
    g.add_edge(START, "look")
    g.add_edge("look", "judge")
    g.add_conditional_edges("judge", after_judge,
                            {"look": "look", "ask": "ask", "answer": "answer"})
    g.add_conditional_edges("ask", after_ask, {"look": "look", "answer": "answer"})
    g.add_edge("answer", END)
    return g
