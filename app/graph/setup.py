"""The setup graph: read a guide, get the access it needs, do it, prove it.

    read ──► access ──► steps ──► validate ──► close
              │  ▲ │
              │  │ └─► (pending approval: END, next tick looks again)
              │  └── request_gate ⏸  (his yes, when the request is not pre-authorized)
              └────► login_gate ⏸    (an expired sign-in: he signs in, never Asta)

The story this is for: a shared document says how to onboard to a system, the
access it needs is missing, someone has to approve a request, and only then can
the setup be done. A chat turn opens the URL and calls that done. It is not done
until the thing works, and between the request and the approval there can be a
day and a restart.

Rules, each a failure the fake portal in the tests makes happen:

  OPENING A PAGE OR CLICKING SUBMIT IS NOT COMPLETION. Each step is checked
  after it is done, and the whole setup is validated independently at the end.
  The outcome is either the verified result or the concrete thing still missing.

  ONE REQUEST, EVER. The receipt is claimed before submitting. A submit whose
  confirmation was lost is looked up in the portal before anything else; found,
  its id is recorded; not found, it is reported as uncertain — never re-submitted
  blind, because two access requests is two approvers' time.

  A SIGN-IN IS HIS. An expired session parks at a gate that asks him to sign in.
  Asta never types a password or stores one; evidence is scrubbed of secrets.

  A CHANGED PAGE STOPS THE RUN. When the portal no longer looks the way the
  adapter expects, or a redirect leaves the allowed hosts, the run stops with
  what it saw. Guessing at a new layout is how the wrong form gets submitted.

  PERMISSION IS EXPLICIT. Requesting access is an outward act: it goes ahead on
  its own only when his instruction authorized it (`authorized["request_access"]`),
  otherwise it waits at a gate for his yes. Setup steps need `authorized["setup"]`.

The portal is an adapter, looked up by name so a resumed thread finds it again:
a fake in the tests now; a browser adapter over `app/sites.py` for a live pilot,
once he names the real document, account and environment.
"""

from __future__ import annotations

import re
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app import store


class Setup(TypedDict, total=False):
    id: str
    portal: str               # adapter name in PORTALS
    doc: str                  # where the guide is (a URL or a document reference)
    goal: str
    authorized: dict          # {"request_access": bool, "setup": bool}, from HIS words
    plan: dict                # {"needs": [...], "steps": [{"id", ...}], "goal": ...}
    granted: list
    pending: list             # access requested, waiting on someone else
    evidence: list            # [{"at", "node", "what"}], secrets scrubbed
    need: str                 # "" | "login" | "request:<name>"
    answer: dict              # his answer at a gate
    outcome: str              # done | blocked | waiting | declined
    detail: str


class LoginExpired(RuntimeError):
    """The portal session is gone. He signs in; nothing here does."""


class PageChanged(RuntimeError):
    """The page no longer matches what the adapter knows. Stop; do not guess."""


class LostReceipt(RuntimeError):
    """The request may have been submitted, but no confirmation came back."""


class OffSite(RuntimeError):
    """A redirect left the hosts this workflow may use."""


#: name -> adapter. An adapter provides (async or sync):
#:   read_document(doc) -> {"text": str, "url": str}
#:   extract(text) -> {"needs": [str], "steps": [{"id": str, "once": bool, ...}], "goal": str}
#:   has_access(name) -> bool
#:   find_request(name) -> {"id": str, "state": "pending"|"approved"|"rejected"} | None
#:   request_access(name, why) -> {"id": str}
#:   step_done(step) -> bool        do_step(step) -> dict
#:   validate(goal) -> {"ok": bool, "detail": str}
PORTALS: dict = {}

_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|cookie|authorization|session|"
                     r"api[_-]?key)\b(\s*[:=]\s*|\s+)\S+")


def _scrub(text: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}=[removed]", str(text or ""))[:500]


def _note(state: Setup, node: str, what: str) -> list:
    return list(state.get("evidence") or []) + [
        {"at": time.time(), "node": node, "what": _scrub(what)}]


async def _call(fn, *args):
    out = fn(*args)
    if hasattr(out, "__await__"):
        out = await out
    return out


def _portal(state: Setup):
    portal = PORTALS.get(state.get("portal", ""))
    if portal is None:
        raise PageChanged(f"no portal adapter '{state.get('portal', '')}' in this process")
    return portal


def _receipt(state: Setup, act: str, name: str) -> str:
    return f"setup_receipt:{state['id']}:{act}:{name}"


def _claim(key: str, detail: dict) -> bool:
    import json
    with store._connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES (?,?)",
                           (key, json.dumps({**detail, "state": "sending", "at": time.time()})))
        return cur.rowcount == 1


def _set(key: str, **detail) -> None:
    import json
    store.kv_set(key, json.dumps({**detail, "at": time.time()}))


def _get(key: str) -> dict:
    import json
    try:
        return json.loads(store.kv_get(key) or "{}")
    except ValueError:
        return {}


def _stop(state: Setup, node: str, exc: Exception) -> dict:
    """A failure that ends or parks the run, said as what it is."""
    if isinstance(exc, LoginExpired):
        return {"need": "login", "evidence": _note(state, node, f"sign-in expired: {exc}")}
    why = {PageChanged: "the page changed", OffSite: "redirected off the allowed site"}.get(
        type(exc), "the portal failed")
    return {"outcome": "blocked", "detail": f"{why}: {_scrub(str(exc))}",
            "evidence": _note(state, node, f"{why}: {exc}")}


# --- nodes --------------------------------------------------------------------

async def read(state: Setup) -> dict:
    if state.get("plan"):
        return {"need": ""}                       # resumed: the guide was read already
    try:
        portal = _portal(state)
        doc = await _call(portal.read_document, state.get("doc", ""))
        plan = await _call(portal.extract, doc.get("text", ""))
    except Exception as exc:                          # noqa: BLE001
        return _stop(state, "read", exc)
    if not plan.get("steps"):
        return {"outcome": "blocked", "detail": "the guide names no setup steps",
                "evidence": _note(state, "read", f"read {doc.get('url', '')}: no steps")}
    return {"plan": plan, "need": "",
            "evidence": _note(state, "read", f"read {doc.get('url', '')}: needs "
                              f"{', '.join(plan.get('needs') or []) or 'no access'}; "
                              f"{len(plan['steps'])} steps")}


async def access(state: Setup) -> dict:
    """Which access is missing, and one request per missing thing — never two."""
    if state.get("outcome") == "blocked":
        return {}
    portal = _portal(state)
    granted, pending, evidence = [], [], list(state.get("evidence") or [])
    answer = state.get("answer") or {}
    try:
        for name in (state.get("plan") or {}).get("needs") or []:
            if await _call(portal.has_access, name):
                granted.append(name)
                continue
            key = _receipt(state, "request", name)
            prior = _get(key)
            if prior:
                # Requested before (this run or before a restart). Look, never resubmit.
                found = await _call(portal.find_request, name)
                if found and found.get("state") == "rejected":
                    return {"outcome": "blocked", "granted": granted,
                            "detail": f"access request for {name} was rejected ({found.get('id')})",
                            "evidence": evidence + [{"at": time.time(), "node": "access",
                                                     "what": f"{name}: rejected"}]}
                if found and prior.get("state") == "uncertain":
                    _set(key, state="sent", id=found.get("id"), reconciled=True)
                elif not found and prior.get("state") == "uncertain":
                    return {"outcome": "blocked", "granted": granted,
                            "detail": f"the access request for {name} may or may not have been "
                                      "submitted, and the portal shows none. Check it there — "
                                      "it will not be submitted again on its own",
                            "evidence": evidence}
                pending.append(name)
                continue
            if not (state.get("authorized") or {}).get("request_access") \
                    and answer.get("gate") != f"request:{name}":
                return {"need": f"request:{name}", "granted": granted, "evidence": evidence}
            if answer.get("gate") == f"request:{name}" and not answer.get("yes"):
                return {"outcome": "declined", "granted": granted,
                        "detail": f"he did not want access to {name} requested",
                        "evidence": evidence}
            if not _claim(key, {"name": name}):
                pending.append(name)
                continue
            # Look first even on a fresh claim: a request someone else (or an
            # earlier, unrecorded attempt) already made is the request.
            found = await _call(portal.find_request, name)
            if found and found.get("state") in ("pending", "approved"):
                _set(key, state="sent", id=found.get("id"), adopted=True)
                pending.append(name)
                evidence.append({"at": time.time(), "node": "access",
                                 "what": f"{name}: existing request {found.get('id')} adopted"})
                continue
            try:
                made = await _call(portal.request_access, name, state.get("goal", ""))
            except LostReceipt as exc:
                _set(key, state="uncertain", why=_scrub(str(exc)))
                found = await _call(portal.find_request, name)
                if not found:
                    return {"outcome": "blocked", "granted": granted,
                            "detail": f"the access request for {name} was submitted without a "
                                      "confirmation and the portal does not show it. Check it "
                                      "there — it will not be submitted again on its own",
                            "evidence": evidence}
                _set(key, state="sent", id=found.get("id"), reconciled=True)
                made = found
            else:
                _set(key, state="sent", id=made.get("id"))
            pending.append(name)
            evidence.append({"at": time.time(), "node": "access",
                             "what": f"{name}: requested once ({made.get('id')})"})
    except Exception as exc:                          # noqa: BLE001
        return _stop(state, "access", exc)
    return {"granted": granted, "pending": pending, "need": "", "evidence": evidence,
            "outcome": "waiting" if pending else ""}


async def steps(state: Setup) -> dict:
    """Each step: skip what is already true, do it, then check it took effect."""
    if not (state.get("authorized") or {}).get("setup"):
        return {"outcome": "blocked", "detail": "setup steps were not authorized"}
    portal = _portal(state)
    evidence = list(state.get("evidence") or [])
    try:
        for step in (state.get("plan") or {}).get("steps") or []:
            sid = str(step.get("id"))
            key = _receipt(state, "step", sid)
            if await _call(portal.step_done, step):
                if _get(key).get("state") != "done":
                    _set(key, state="done")
                    evidence.append({"at": time.time(), "node": "steps",
                                     "what": f"{sid}: already in place"})
                continue
            prior = _get(key)
            if prior.get("state") in ("sending", "uncertain") and step.get("once"):
                return {"outcome": "blocked", "evidence": evidence,
                        "detail": f"step {sid} may have been submitted and is not in effect; it "
                                  "submits something, so it is not repeated on its own"}
            _set(key, state="sending")
            try:
                done = await _call(portal.do_step, step) or {}
            except (LoginExpired, PageChanged, OffSite):
                _set(key, state="uncertain")
                raise
            if not await _call(portal.step_done, step):
                _set(key, state="uncertain")
                return {"outcome": "blocked", "evidence": evidence,
                        "detail": f"step {sid} ran but did not take effect: "
                                  f"{_scrub(done.get('detail', ''))}"}
            _set(key, state="done")
            evidence.append({"at": time.time(), "node": "steps", "what": f"{sid}: done, checked"})
    except Exception as exc:                          # noqa: BLE001
        return _stop(state, "steps", exc)
    return {"evidence": evidence, "need": ""}


async def validate(state: Setup) -> dict:
    if state.get("outcome") == "blocked" or state.get("need"):
        return {}
    try:
        seen = await _call(_portal(state).validate, state.get("goal", "")) or {}
    except Exception as exc:                          # noqa: BLE001
        return _stop(state, "validate", exc)
    if seen.get("ok"):
        return {"outcome": "done", "detail": _scrub(seen.get("detail", "")),
                "evidence": _note(state, "validate", f"verified: {seen.get('detail', '')}")}
    return {"outcome": "blocked", "detail": f"setup failed validation: {_scrub(seen.get('detail', ''))}",
            "evidence": _note(state, "validate", f"failed: {seen.get('detail', '')}")}


def login_gate(state: Setup) -> dict:
    return {"answer": interrupt({"gate": "login", "portal": state.get("portal", ""),
                                 "ask": "Your sign-in to the portal expired. Sign in, then say done."}),
            "need": ""}


def request_gate(state: Setup) -> dict:
    name = (state.get("need") or "").split(":", 1)[-1]
    said = interrupt({"gate": f"request:{name}", "goal": state.get("goal", ""),
                      "ask": f"Request access to {name} for: {state.get('goal', '')}?"})
    return {"answer": {**(said or {}), "gate": f"request:{name}"}, "need": ""}


def close(state: Setup) -> dict:
    store.record_outcome("setup", state.get("outcome") or "ended", subject=state.get("id", ""),
                         detail=(state.get("detail") or "")[:200])
    return {}


# --- routing ------------------------------------------------------------------

def _after(state: Setup) -> str:
    if state.get("need") == "login":
        return "login_gate"
    if (state.get("need") or "").startswith("request:"):
        return "request_gate"
    if state.get("outcome") in ("blocked", "declined", "done"):
        return "close"
    return ""


def after_read(state: Setup) -> str:
    return _after(state) or "access"


def after_access(state: Setup) -> str:
    nxt = _after(state)
    if nxt:
        return nxt
    return "wait" if state.get("pending") else "steps"


def after_steps(state: Setup) -> str:
    return _after(state) or "validate"


def build() -> StateGraph:
    g = StateGraph(Setup)
    for name, fn in (("read", read), ("access", access), ("steps", steps),
                     ("validate", validate), ("login_gate", login_gate),
                     ("request_gate", request_gate), ("close", close)):
        g.add_node(name, fn)
    g.add_edge(START, "read")
    gates = {"login_gate": "login_gate", "request_gate": "request_gate", "close": "close"}
    g.add_conditional_edges("read", after_read, {**gates, "access": "access"})
    # Waiting on someone else's approval ends the run; the next tick starts again
    # from the top, where every node first checks what is already true.
    g.add_conditional_edges("access", after_access, {**gates, "wait": END, "steps": "steps"})
    g.add_conditional_edges("steps", after_steps, {**gates, "validate": "validate"})
    g.add_edge("validate", "close")
    g.add_edge("login_gate", "read")
    g.add_edge("request_gate", "access")
    g.add_edge("close", END)
    return g
