"""The draft-and-send graph: his yes, kept safely, and the send verified after.

    draft ──► stage ──► gate ⏸ ──► send ──► confirm ──► record
                          │                    │
                          ├─► rewrite ─► stage │
                          └─► drop             └─► say it did not go

This is the one act Asta cannot take back, so it is the one with the most
machinery around it, and every piece of that machinery is here because something
went wrong without it:

  HIS YES IS WRITTEN DOWN BEFORE IT IS APPLIED. On 13 September an approval
  arrived moments before a restart and was lost twice — the process that would
  have acted on it was gone, and the thread was still waiting for it. `engine`
  records the answer first; this graph simply parks at a gate and lets it.

  "NO" GOES BACK TO THE DRAFT, NOT TO A BRAIN. A rejection was once handed to a
  model that re-staged the same message with different adjectives. Here "no with
  words" re-enters `draft` carrying what he said, and "no" alone drops it.

  A SEND IS CONFIRMED BY LOOKING. The door reports what it did; `confirm` reads
  the outcome back and a send that cannot be confirmed is reported as NOT sent.
  "Sent" that only means "the function returned" is how a message nobody received
  became something he found out about from the person who never got it.

The graph owns the SEQUENCE. The staging itself, the policy check and the
authority ledger stay where they are — one shared decision, not a second copy
that drifts (the 20-minute constant, again).
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class Send(TypedDict, total=False):
    to: str              # who it goes to
    channel: str         # teams | whatsapp | outlook
    subject: str
    body: str            # the draft as it stands
    feedback: str        # what he said when he sent it back
    answer: dict         # his word at the gate
    staged: bool
    sent: bool
    confirmed: str       # what the door said afterwards
    outcome: str         # sent | dropped | failed


#: Injected by the caller. `WRITER(state)` returns the body; `SENDER(state)`
#: performs the act and returns what the door said; `CONFIRMER(state)` reads it
#: back. Injected rather than imported so the sequence can be tested without
#: touching a colleague, and so one graph serves every channel.
WRITER = None
SENDER = None
CONFIRMER = None


async def _call(fn, state):
    if fn is None:
        return ""
    out = fn(dict(state))
    if hasattr(out, "__await__"):
        out = await out
    return out


async def draft(state: Send) -> dict:
    """Write it, or rewrite it with what he said — never from nothing twice."""
    body = await _call(WRITER, state)
    return {"body": body or state.get("body", ""), "feedback": ""}


async def stage(state: Send) -> dict:
    """Put it in front of him. Nothing leaves the house from this node."""
    return {"staged": True}


def gate(state: Send) -> dict:
    """Only waits — a resumed node re-runs from its start."""
    return {"answer": interrupt({"gate": "send",
                                 "to": state.get("to", ""),
                                 "channel": state.get("channel", ""),
                                 "body": state.get("body", "")})}


def after_gate(state: Send) -> str:
    said = state.get("answer") or {}
    if said.get("approved"):
        return "send"
    text = (said.get("text") or "").strip()
    # "no, say it differently" is feedback on THIS draft; "no" is a decision.
    return "draft" if text else "drop"


async def send(state: Send) -> dict:
    out = await _call(SENDER, state)
    return {"sent": bool(out), "confirmed": str(out or "")}


async def confirm(state: Send) -> dict:
    """Read the act back. A send that cannot be confirmed did not happen."""
    seen = await _call(CONFIRMER, state)
    ok = bool(seen) if CONFIRMER is not None else bool(state.get("sent"))
    return {"confirmed": str(seen or state.get("confirmed", "")),
            "outcome": "sent" if ok else "failed"}


async def drop(state: Send) -> dict:
    return {"outcome": "dropped", "sent": False}


def build() -> StateGraph:
    g = StateGraph(Send)
    g.add_node("draft", draft)
    g.add_node("stage", stage)
    g.add_node("gate", gate)
    g.add_node("send", send)
    g.add_node("confirm", confirm)
    g.add_node("drop", drop)
    g.add_edge(START, "draft")
    g.add_edge("draft", "stage")
    g.add_edge("stage", "gate")
    g.add_conditional_edges("gate", after_gate,
                            {"send": "send", "draft": "draft", "drop": "drop"})
    g.add_edge("send", "confirm")
    g.add_edge("confirm", END)
    g.add_edge("drop", END)
    return g
