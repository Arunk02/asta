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
    if not pending and status.lower().endswith("failed"):
        lines.append("  it failed — read the history for the event that ended it")
    return "\n".join(lines)


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
