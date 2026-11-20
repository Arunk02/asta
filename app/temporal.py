"""Temporal, as one of Asta's own capabilities rather than a tool nobody was given.

Measured on 22 Sep: his `temporal-mcp-proxy.py` and its certificates work — prod
answered with 834 running workflows in 3.9 s — and there were **zero** Temporal
calls in three weeks of sessions. The proxy is declared in asta's mcp.json, which
reaches the in-process agent; the CLI brains doing the investigating never had it.
A capability here is attached to every brain by construction.

The shape is the proxy's, ported: he names an environment, and the env map turns
that into a cluster address, a namespace and the mTLS certificate pair, so no
model ever has to remember any of them. The `temporal` CLI does the gRPC, mTLS and
protobuf — it is a static binary that already speaks them correctly.

Read-only, by an allowlist: list, count, describe, history. There is no
passthrough, and the CLI is always run as an argv list with no shell, so a
workflow id or a query is an opaque argument and never something a shell parses.

The map itself names his clusters and namespaces, so it lives beside the database
(data/temporal-envs.json) rather than in this public repository.
"""

from __future__ import annotations

import asyncio
import json
import re
import os
import shutil
from pathlib import Path

TIMEOUT = 60.0
CONFIG_DIR = Path(os.path.expanduser(os.environ.get("ASTA_TEMPORAL_CONFIG_DIR",
                                                    "~/.config/temporal-mcp")))


def cli() -> str:
    return os.environ.get("ASTA_TEMPORAL_CLI") or shutil.which("temporal") or "temporal"


def _map_path() -> Path:
    named = (os.environ.get("ASTA_TEMPORAL_ENVS_FILE") or "").strip()
    if named:
        return Path(os.path.expanduser(named))
    from . import store
    return Path(store.DB_PATH).parent / "temporal-envs.json"


def envs() -> dict[str, dict]:
    """{env: {address, namespace, cert}} — his clusters, from his own file."""
    inline = (os.environ.get("ASTA_TEMPORAL_ENVS") or "").strip()
    raw = inline or (_map_path().read_text() if _map_path().exists() else "")
    if not raw:
        return {}
    try:
        found = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): v for k, v in found.items() if isinstance(v, dict)}


def enabled() -> bool:
    return bool(envs())


class TemporalError(RuntimeError):
    """What went wrong, in words he can act on."""


def certs(name: str) -> tuple[Path, Path]:
    """(certificate, key) for a cert name; an env var overrides the default path."""
    up = name.upper()
    cert = os.environ.get(f"TEMPORAL_{up}_CERT") or str(CONFIG_DIR / f"{name}.pem")
    key = os.environ.get(f"TEMPORAL_{up}_KEY") or str(CONFIG_DIR / f"{name}.key")
    return Path(os.path.expanduser(cert)), Path(os.path.expanduser(key))


def _connection(env: str) -> list[str]:
    """The CLI flags for one environment, or a refusal that says how to fix it."""
    known = envs()
    if env not in known:
        raise TemporalError(f"env must be one of {', '.join(known) or '(none configured)'}; "
                            f"got {env!r}")
    spec = known[env]
    cert, key = certs(str(spec.get("cert") or env))
    if not (cert.exists() and key.exists()):
        # Certificates are namespace-scoped (CN=<namespace>:write), so an env that
        # does not share a namespace needs its own. Say where it comes from rather
        # than reporting a connection failure he cannot act on.
        hint = spec.get("vault") or ""
        raise TemporalError(
            f"no certificate for {env} (expected {cert} and {key.name}). "
            + (f"It comes from Vault: {hint}" if hint else
               "Fetch it from Vault into ~/.config/temporal-mcp/."))
    return ["--address", str(spec.get("address", "")),
            "--namespace", str(spec.get("namespace", "")),
            "--tls", "--tls-cert-path", str(cert), "--tls-key-path", str(key)]


async def _run(args: list[str]) -> str:
    binary = cli()
    if not (shutil.which(binary) or os.path.exists(binary)):
        raise TemporalError("the temporal CLI is not on this machine "
                            "(brew install temporal), so nothing can be read")
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise TemporalError(f"temporal did not answer within {int(TIMEOUT)}s")
    said, problem = out.decode(errors="replace").strip(), err.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise TemporalError((problem or said or f"exit {proc.returncode}")[:400])
    # The CLI writes warnings to stderr even when it worked; keep them only when
    # there is nothing else, so a warning never masquerades as the answer.
    return said or problem


async def count(env: str, query: str = "") -> int:
    """How many workflows match. The cheapest question, so ask it first."""
    said = await _run([cli(), "workflow", "count"] + _connection(env)
                      + ["--query", query or 'ExecutionStatus="Running"'])
    digits = [int(w) for w in said.replace(",", " ").split() if w.isdigit()]
    return digits[0] if digits else 0


async def workflows(env: str, query: str = "", limit: int = 20) -> list[dict]:
    """Matching workflows, newest first, as rows rather than a wall of JSON."""
    said = await _run([cli(), "workflow", "list"] + _connection(env)
                      + ["--query", query or 'ExecutionStatus="Failed"',
                         "--limit", str(max(1, min(int(limit or 20), 100))),
                         "--output", "json"])
    return _rows(said)


async def describe(env: str, workflow_id: str, run_id: str = "") -> dict:
    """One workflow: status, times, and what it is waiting on."""
    if not workflow_id:
        raise TemporalError("which workflow? a workflow id is needed")
    args = ([cli(), "workflow", "describe"] + _connection(env)
            + ["--workflow-id", workflow_id, "--output", "json"])
    if run_id:
        args += ["--run-id", run_id]
    said = await _run(args)
    try:
        return json.loads(said)
    except ValueError:
        return {"raw": said[:4000]}


async def history(env: str, workflow_id: str, run_id: str = "") -> list[dict]:
    """The workflow's own events — where it actually stopped."""
    if not workflow_id:
        raise TemporalError("which workflow? a workflow id is needed")
    args = ([cli(), "workflow", "show"] + _connection(env)
            + ["--workflow-id", workflow_id, "--output", "json"])
    if run_id:
        args += ["--run-id", run_id]
    return _rows(await _run(args), key="events")


def _rows(said: str, key: str = "") -> list[dict]:
    """The CLI answers with a list, or an object holding one, or JSON lines."""
    said = (said or "").strip()
    if not said:
        return []
    try:
        found = json.loads(said)
    except ValueError:
        out = []
        for line in said.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
    if isinstance(found, list):
        return found
    if isinstance(found, dict):
        for name in ([key] if key else []) + ["executions", "events", "items", "data"]:
            if isinstance(found.get(name), list):
                return found[name]
        return [found]
    return []


# --- what he reads --------------------------------------------------------------------

def _of(row: dict, *path: str) -> str:
    """A value from Temporal's nested JSON, by the names it actually uses."""
    at: object = row
    for step in path:
        if not isinstance(at, dict):
            return ""
        at = at.get(step, "")
    return str(at or "")


def render(rows: list[dict], env: str, query: str = "", limit: int = 10) -> str:
    if not rows:
        return f"No workflows in {env} matching {query or 'the default query'}."
    head = f"{len(rows)} workflow(s) in {env}" + (f" matching {query}" if query else "")
    out = [head + ":"]
    for row in rows[:limit]:
        wid = (_of(row, "execution", "workflowId") or _of(row, "workflowExecutionInfo",
                                                          "execution", "workflowId")
               or _of(row, "workflowId") or "?")
        kind = (_of(row, "type", "name") or _of(row, "workflowType", "name")
                or _of(row, "workflowTypeName"))
        status = (_of(row, "status") or _of(row, "executionStatus")
                  or _of(row, "workflowExecutionInfo", "status"))
        started = _of(row, "startTime") or _of(row, "workflowExecutionInfo", "startTime")
        out.append(f"  [{status or '?'}] {kind or '?'} · {wid[:70]}"
                   + (f" · started {started[:19]}" if started else ""))
    if len(rows) > limit:
        out.append(f"  …and {len(rows) - limit} more")
    return "\n".join(out)


def render_one(found: dict, env: str) -> str:
    """One workflow, and — when it failed — the reason, which is the whole point."""
    info = found.get("workflowExecutionInfo") or found
    wid = _of(info, "execution", "workflowId") or _of(info, "workflowId") or "?"
    status = _of(info, "status") or _of(info, "executionStatus") or "?"
    kind = _of(info, "type", "name") or _of(info, "workflowTypeName") or "?"
    lines = [f"{kind} · {wid} in {env}: {status}",
             f"  started {(_of(info, 'startTime') or '?')[:19]}"
             + (f", closed {_of(info, 'closeTime')[:19]}" if _of(info, "closeTime") else "")]
    pending = found.get("pendingActivities") or []
    for activity in pending[:5]:
        lines.append(f"  waiting on {_of(activity, 'activityType', 'name') or 'an activity'}"
                     f" · attempt {_of(activity, 'attempt') or '?'}"
                     + (f" · last failure: {_of(activity, 'lastFailure', 'message')[:200]}"
                        if _of(activity, "lastFailure", "message") else ""))
    lines += _size_and_close(found, info)
    if not pending and status.lower().endswith("failed") and not found.get("closeEvent"):
        lines.append("  it failed — read the history for the event that ended it")
    return "\n".join(lines)


#: Temporal's own history limits: it warns at 10k events / 10 MB and terminates
#: the run at 51,200 events / 50 MB. A run near them replays slowly, misses its
#: workflow-task timeout, and looks "stuck" for reasons no log line names.
_WARN_EVENTS, _MAX_EVENTS = 10_000, 51_200
_WARN_BYTES, _MAX_BYTES = 10 * 2**20, 50 * 2**20


def _size_and_close(found: dict, info: dict) -> list[str]:
    """History size against Temporal's limits, the task timeout, and who/what closed it.

    12 Nov, a prod booking: the describe carried a 47.9 MB history and "Terminated
    from the Web UI by …", and the answer said the cause was "not confirmed" —
    these lines were never rendered, so the brain never saw them."""
    out = []
    try:
        events = int(_of(info, "historyLength") or 0)
        size = int(_of(info, "historySizeBytes") or 0)
    except ValueError:
        events = size = 0
    if events or size:
        line = f"  history: {events} events, {size / 2**20:.1f} MB"
        if events >= _WARN_EVENTS or size >= _WARN_BYTES:
            line += (f" — past Temporal's warning ({_WARN_EVENTS} events / 10 MB; it terminates "
                     f"at {_MAX_EVENTS} / 50 MB): replays get slow and workflow tasks time out; "
                     f"check continueAsNew")
        out.append(line)
    timeout = _of(found, "executionConfig", "defaultWorkflowTaskTimeout")
    if timeout:
        out.append(f"  workflow task timeout {timeout}")
    close = found.get("closeEvent") or {}
    if isinstance(close, dict) and close:
        kind = (close.get("eventType") or "").replace("EVENT_TYPE_WORKFLOW_EXECUTION_", "").lower()
        attrs = next((v for k, v in close.items() if k.endswith("EventAttributes")
                      and isinstance(v, dict)), {})
        why = (_of(attrs, "reason") or _of(attrs, "failure", "message")
               or _of(attrs, "failure", "cause", "message"))
        who = _of(attrs, "identity")
        out.append(f"  closed by: {kind or 'close event'} at {(close.get('eventTime') or '?')[:19]}"
                   + (f" · reason: {why[:200]}" if why else "")
                   + (f" · by {who}" if who and who not in why else ""))
    return out


_LINK = re.compile(r"https?://[^\s/]*service-composition[^\s/]*/namespaces/([^/\s]+)/workflows/"
                   r"([^/\s?#…]+)(?:/([0-9a-f-]{20,}))?", re.I)


def parse_link(text: str) -> tuple[str, str, str] | None:
    """(env, workflow id, run id) from a Temporal web link, or None.
    13 Oct: Vinish shared one; Asta asked for "the workflow ID" instead."""
    m = _LINK.search(text or "")
    if not m:
        return None
    ns, wid, run = m.group(1), m.group(2), m.group(3) or ""
    env = next((e for e, v in envs().items() if v.get("namespace") == ns), "")
    if not env:
        env = "prod" if "prod" in ns and "pre" not in ns else ""
    return env, wid, run


def _payloads(attrs: dict) -> str:
    """A start's or signal's input, decoded: the payload is the evidence. The ack
    for H7Y5BWNBNX9 was proved by the uniqueIdentifier inside this (12 Oct)."""
    import base64
    out = []
    for p in ((attrs.get("input") or {}).get("payloads") or [])[:3]:
        data = p.get("data", "") if isinstance(p, dict) else ""
        try:
            data = base64.b64decode(data).decode("utf-8", "replace")
        except Exception:                                          # noqa: BLE001
            pass
        out.append(" ".join(str(data).split())[:400])
    return " | ".join(out)


#: What an event's input says happened — the rest of the payload is detail.
#: Not `executionStarted`: the transport order inside the service plan keeps it
#: false even after TMS reported EXECUTED (18 Nov — an answer said "TMS never
#: started" from it). TMS's own word is the EXECUTION work processes, below.
_EVENT_KEYS = ("eventName", "eventType", "bookingStatus", "status", "workOrderStatus",
               "transportOrderNumber", "version", "isCancellation",
               "workProcessName", "workProcessStatus")
BOOKING_RUNS = 60


def _facts(obj, depth: int = 0, found: dict | None = None) -> dict:
    """The first value of each _EVENT_KEYS key, searched a few levels deep."""
    found = {} if found is None else found
    if depth > 3:
        return found
    if isinstance(obj, dict):
        for k in _EVENT_KEYS:
            v = obj.get(k)
            if k not in found and isinstance(v, (str, int, float, bool)) and v != "":
                found[k] = v
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _facts(v, depth + 1, found)
    elif isinstance(obj, list):
        for v in obj[:5]:
            _facts(v, depth + 1, found)
    return found


def _booking_refs(obj, depth: int = 0) -> list[str] | None:
    """The booking-level reference types in an input (`…/booking/references`), or None."""
    if depth > 4:
        return None
    if isinstance(obj, dict):
        booking = obj.get("booking")
        if isinstance(booking, dict) and isinstance(booking.get("references"), list):
            return sorted({str(r.get("referenceType") or r.get("referenceTypeEnum") or "")
                           for r in booking["references"] if isinstance(r, dict)} - {""})
        for v in obj.values():
            found = _booking_refs(v, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj[:3]:
            found = _booking_refs(v, depth + 1)
            if found is not None:
                return found
    return None


def _executions(obj, out: dict | None = None, depth: int = 0) -> dict:
    """{work process: status} for the EXECUTION work processes TMS reported —
    CONTAINER_DELIVERY_EXECUTION=EXECUTED — read from the new service plan."""
    out = {} if out is None else out
    if depth > 8:
        return out
    if isinstance(obj, dict):
        name, status = obj.get("workProcessName"), obj.get("workProcessStatus")
        if isinstance(name, str) and "EXECUTION" in name and isinstance(status, str):
            out.setdefault(name, status)
        for k, v in obj.items():
            if k != "oldServicePlan" and isinstance(v, (dict, list)):
                _executions(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:20]:
            _executions(v, out, depth + 1)
    return out


def _start_facts(events: list[dict]) -> dict:
    import base64
    first = events[0] if events else {}
    attrs = first.get("workflowExecutionStartedEventAttributes") or {}
    facts: dict = {}
    for p in ((attrs.get("input") or {}).get("payloads") or [])[:3]:
        try:
            value = json.loads(base64.b64decode(p.get("data", "")).decode("utf-8", "replace"))
        except Exception:                                      # noqa: BLE001
            continue
        # bookingEventsWorkflow's first argument IS the event: "SAP_TMS_ACK_FEEDBACK".
        if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{3,60}", value):
            facts.setdefault("eventName", value)
        else:
            facts = _facts(value, found=facts)
            refs = _booking_refs(value)
            if refs is not None and "_refs" not in facts:
                facts["_refs"] = refs
            done = _executions(value.get("newServicePlan") if isinstance(value, dict)
                               and "newServicePlan" in value else value)
            if done and "_executions" not in facts:
                facts["_executions"] = done
    return facts


#: The runs that ARE the booking's story: what it received, and what billing made
#: of it. Activity-plan and reporting runs follow from these, so they go first
#: when there are more runs than can be read.
_STORY_TYPES = ("bookingEventsWorkflow", "BillingWorkflow")


def _pick_runs(rows: list[dict], limit: int) -> list[dict]:
    if len(rows) <= limit:
        return rows
    kind = lambda r: _of(r, "type", "name") or _of(r, "workflowType", "name")
    story = [r for r in rows if kind(r) in _STORY_TYPES]
    if len(story) > limit:
        story = story[:10] + story[-(limit - 10):]
    rest = [r for r in rows if kind(r) not in _STORY_TYPES]
    chosen = story + rest[-max(0, limit - len(story)):] if len(story) < limit else story
    when = lambda r: _of(r, "startTime") or _of(r, "workflowExecutionInfo", "startTime")
    return sorted(chosen, key=when)


async def booking_events(env: str, booking: str, limit: int = BOOKING_RUNS) -> dict:
    """Every workflow run for one booking, oldest first, each with what its start
    input says happened — the event, the status, the TO and its version.

    18 Nov: "did TMS send the execution status?" was answered precisely only by
    listing EVERY event the booking received, decoded — two ACKs, no execution
    status. One workflow at a time, an answer sampled 6 of 287 runs and guessed."""
    query = f'WorkflowId STARTS_WITH "{booking}"'
    rows = await workflows(env, query, limit=100)
    total = await count(env, query) if len(rows) >= 100 else len(rows)
    rows = sorted(rows, key=lambda r: _of(r, "startTime") or _of(r, "workflowExecutionInfo", "startTime"))
    picked = _pick_runs(rows, limit)
    gate = asyncio.Semaphore(6)

    async def one(row: dict) -> dict:
        wid = _of(row, "execution", "workflowId") or _of(row, "workflowId")
        run = _of(row, "execution", "runId") or _of(row, "runId")
        async with gate:
            try:
                facts = _start_facts(await history(env, wid, run))
            except TemporalError as exc:
                facts = {"error": str(exc)[:80]}
        return {"started": (_of(row, "startTime") or "")[:19], "id": wid,
                "type": _of(row, "type", "name") or _of(row, "workflowType", "name"),
                "status": (_of(row, "status") or "").replace("WORKFLOW_EXECUTION_STATUS_", ""),
                "facts": facts}

    runs = await asyncio.gather(*(one(r) for r in picked))
    return {"env": env, "booking": booking, "total": total, "listed": len(rows), "runs": runs}


def _seconds(stamp: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat((stamp or "")[:19]).timestamp()
    except ValueError:
        return 0.0


#: Values that mean something failed or is waiting — a field moving INTO one of
#: these is a step backward, whatever the field is.
_BAD = re.compile(r"FAIL|REJECT|PENDING|ERROR|CANCEL|TERMINATED|TIMED_OUT", re.I)
#: TMS's execution progress; moving left is going backward.
_PROGRESS = ("NOT_STARTED", "IN_EXECUTION", "EXECUTED")
#: Fields that change from run to run by nature, not because something broke.
_VOLATILE = {"eventName", "eventType", "version", "transportOrderNumber"}


def _state(r: dict) -> dict:
    """What a run says the booking IS: its references, TMS's execution statuses,
    its run status and its scalar fields — one flat map to compare runs by."""
    f = r["facts"]
    state = {f"ref {x}": "present" for x in f.get("_refs") or []}
    state.update({f"TMS {k.replace('_EXECUTION', '')}": v
                  for k, v in (f.get("_executions") or {}).items()})
    state.update({k: str(v) for k, v in f.items()
                  if not k.startswith("_") and k not in _VOLATILE})
    if r.get("status"):
        state["run"] = r["status"]
    return state


def _got_worse(old: dict, new: dict, same_event: bool) -> list[str]:
    """What the newer run lost or moved backward on, against the older run."""
    out = []
    # A run that says nothing about a kind of state (no references at all, no TMS
    # status at all) is not losing it — it is another kind of event. Only a run
    # that carries the kind, and carries less of it, lost something.
    speaks = {kind for kind in ("ref ", "TMS ") if any(k.startswith(kind) for k in new)}
    for k, was in old.items():
        now = new.get(k)
        kind = next((x for x in ("ref ", "TMS ") if k.startswith(x)), "")
        if now is None:
            if (kind and kind in speaks) or (not kind and same_event and k != "run"):
                out.append(f"lost {k}")
        elif now != was:
            backward = (was in _PROGRESS and now in _PROGRESS
                        and _PROGRESS.index(now) < _PROGRESS.index(was))
            if backward or (_BAD.search(now) and not _BAD.search(was)):
                out.append(f"{k} {was}→{now}")
    return out


def regressions(runs: list[dict]) -> list[dict]:
    """Every run that is worse than the run of the same workflow before it, as data:
    when, which run and event, what it lost or moved backward on, and the event the
    booking received just before it. One source for the summary the brain reads and
    for the check on its answer (answers.ignores_regression)."""
    last: dict[str, dict] = {}
    out: list[dict] = []
    for i, r in enumerate(runs):
        state, prev = _state(r), last.get(r["type"])
        if prev is not None:
            worse = _got_worse(prev["state"], state,
                               prev["event"] == r["facts"].get("eventName"))
            if worse:
                after = _event_before(runs, i)
                out.append({"started": r["started"], "at": _seconds(r["started"]),
                            "run": r["type"], "event": r["facts"].get("eventName") or "",
                            "items": worse,
                            "after": after[1]["facts"]["eventName"] if after else "",
                            "after_started": after[1]["started"] if after else "",
                            "gap": int(after[0]) if after else None})
        last[r["type"]] = {"state": state, "event": r["facts"].get("eventName")}
    return out


def _event_before(runs: list[dict], i: int) -> tuple[float, dict] | None:
    """The event the booking received just before run i, within ten minutes —
    what it received itself winning over a run it set off in the same second."""
    r, nearest = runs[i], None
    for j in range(i - 1, -1, -1):
        e = runs[j]
        gap = _seconds(r["started"]) - _seconds(e["started"])
        if gap > 600 or (nearest and gap > nearest[0] + 1):
            break
        if e["type"] != r["type"] and e["facts"].get("eventName"):
            if nearest is None or (e["type"] == "bookingEventsWorkflow"
                                   and nearest[1]["type"] != "bookingEventsWorkflow"):
                nearest = (gap, e)
    return nearest


def regression_line(g: dict) -> str:
    before = (f" — {g['gap']}s after {g['after']} ({g['after_started'][11:19]})"
              if g.get("after") else "")
    return (f"{g['started'].replace('T', ' ')} {g['run']} · {g['event'] or '-'}: "
            f"{'; '.join(g['items'][:8])}{before}")


def _worse_summary(runs: list[dict]) -> list[str]:
    """The regressions, first in the list, where they cannot be missed.

    Any kind of regression, not one kind: 19 Nov it was booking references a
    pricing update did not carry (the line was in the list and one of two answers
    still missed it); next time it may be a status, a date or a TMS milestone."""
    found = regressions(runs)
    if not found:
        return []
    return (["What got worse between runs — where a pass can turn into a fail; compare "
             "what the event before it carried with what was there before:"]
            + [f"  ⚠ {regression_line(g)}" for g in found[-5:]])


def render_booking_events(found: dict) -> str:
    runs = found.get("runs") or []
    if not runs:
        return f"No workflows for {found.get('booking')} in {found.get('env')}."
    from collections import Counter
    seen = Counter(str(r["facts"].get("eventName") or r["type"] or "?") for r in runs)
    head = (f"{found['total']} workflow run(s) for {found['booking']} in {found['env']}"
            + (f" — {len(runs)} shown (the booking's events and billing runs first)"
               if len(runs) < found["total"] else "")
            + ", oldest first, times UTC:")
    out = [head]
    before: dict[str, set[str]] = {}
    for r in runs:
        f = {k: v for k, v in r["facts"].items() if k not in ("eventName", "_refs", "_executions")}
        detail = " ".join(f"{k}={v}" for k, v in f.items())
        done = r["facts"].get("_executions") or {}
        if done:
            detail += (" · " if detail else "") + "TMS: " + " ".join(
                f"{k.replace('_EXECUTION', '')}={v}" for k, v in list(done.items())[:4])
        refs = r["facts"].get("_refs")
        change = ""
        if refs is not None:
            # 18 Nov: an upstream update carried an older copy of the booking's
            # references; what it dropped is what billing then failed on.
            now, prev = set(refs), before.get(r["type"])
            if prev is not None and now != prev:
                change = " · refs " + " ".join([f"+{x}" for x in sorted(now - prev)]
                                               + [f"−{x}" for x in sorted(prev - now)])
            before[r["type"]] = now
        out.append(f"  {r['started'].replace('T', ' ')} [{r['status'] or '?'}] {r['type']} · "
                   f"{r['facts'].get('eventName') or '-'}" + (f" · {detail}" if detail else "")
                   + change)
    summary = _worse_summary(runs)
    if summary:
        out[1:1] = summary
    out.append("Events received: " + ", ".join(f"{k} ×{n}" for k, n in seen.most_common()))
    out.append("An event not in this list did not reach this booking's workflows in this "
               "environment. Before saying the sender never sent it, show the consumer took "
               "the same message for other bookings in the window (log_counts).")
    out.append("If it worked once and failed later, the cause is in the runs between the last "
               "success and the failure: read those runs' input (temporal_workflow "
               "history=true) and compare what they carried with what was stored before.")
    return "\n".join(out)


def render_history(events: list[dict], env: str, workflow_id: str, limit: int = 40) -> str:
    """Every step in order — when, what, and for starts and signals, with what."""
    if not events:
        return f"No history for {workflow_id} in {env}."
    out = [f"{len(events)} events for {workflow_id} in {env} (times UTC):"]
    noise = ("WORKFLOW_TASK_SCHEDULED", "WORKFLOW_TASK_STARTED", "WORKFLOW_TASK_COMPLETED",
             "ACTIVITY_TASK_STARTED")
    for e in events:
        kind = str(e.get("eventType", "?")).replace("EVENT_TYPE_", "")
        if kind in noise:
            continue
        attrs = next((v for k, v in e.items() if k.endswith("Attributes") and isinstance(v, dict)), {})
        name = (_of(attrs, "activityType", "name") or attrs.get("signalName")
                or _of(attrs, "workflowType", "name") or "")
        line = f"  {str(e.get('eventTime', ''))[11:23]} {kind}" + (f" {name}" if name else "")
        if kind in ("WORKFLOW_EXECUTION_STARTED", "WORKFLOW_EXECUTION_SIGNALED"):
            who = attrs.get("identity") or ""
            line += (f" by {who}" if who else "") + (f" · input: {_payloads(attrs)}" if attrs.get("input") else "")
        failure = _of(attrs, "failure", "message")
        if failure:
            line += f" · failure: {failure[:200]}"
        out.append(line)
        if len(out) > limit:
            out.append("  …more — ask for a run_id or a narrower question")
            break
    return "\n".join(out)


async def health() -> tuple[bool, str]:
    known = envs()
    if not known:
        return False, "no environment map (data/temporal-envs.json)"
    missing = [e for e, spec in known.items()
               if not all(p.exists() for p in certs(str(spec.get("cert") or e)))]
    if len(missing) == len(known):
        return False, f"no certificates for any env ({', '.join(sorted(missing))})"
    ready = sorted(set(known) - set(missing))
    tail = f"; no certificate for {', '.join(sorted(missing))}" if missing else ""
    return True, f"ready for {', '.join(ready)}{tail}"
