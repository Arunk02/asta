"""Is the debugging stack actually usable — or does it only look usable.

Arun's three debugging surfaces are Jira (what was reported), Grafana (what the
logs say) and Temporal (what the workflow did). Each reaches a different system
behind corporate auth, and each fails in a way that is easy to mistake for
"nothing wrong":

  - a Temporal cert that EXISTS but is empty, so `os.path.exists` says yes and the
    TLS handshake then dies with "failed to find any PEM data in certificate
    input" — a sentence that says nothing about the actual problem;
  - a cert that parses but expired last week;
  - Grafana reachable but the VPN down, which looks the same as a quiet system;
  - Atlassian OAuth that lapsed, which is the state Jira sat in for weeks.

This is the same failure the August review kept finding: **presence checked
instead of validity.** An `ANTHROPIC_API_KEY` that was set but refused. A pooled
browser assumed alive. Cached history assumed complete. Every one of them read as
healthy right up until the moment it mattered.

The env map is READ FROM THE PROXY, never copied. Two copies of a mapping is how
a new env gets added in one place and silently missing in the other — the same
argument as one shared policy function rather than a constant per brain.

Split by cost, because a check nobody can afford to run is a check that gets
turned off:

  certs()  — local, microseconds. Safe on every health pass.
  reach()  — network, seconds. Daily, or on demand when something looks wrong.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import time
from pathlib import Path

from . import quiet

#: The proxy owns the env -> cluster/namespace/cert mapping. Asta reads it rather
#: than restating it; a second copy is a second thing to forget to update.
#: `or` rather than a get-default, because `.get(key, default)` uses the default
#: only when the key is ABSENT — and a key set to the empty string is exactly what
#: a documented-but-unfilled line in .env produces. Written blank here, it silently
#: pointed at Path("") and disabled every check in this module while reporting
#: nothing. Presence is not validity, one more time.
TEMPORAL_PROXY = Path(
    (os.environ.get("ASTA_TEMPORAL_PROXY") or "").strip()
    or Path.home() / "temporal-mcp-proxy.py")

#: Warn before a cert dies, not after. A cert that expires on a Friday is found on
#: Monday by someone who needed it, which is the worst possible moment.
EXPIRY_WARN_DAYS = 14

#: The canonical fetcher. It lives outside this repo because it encodes internal
#: Vault mounts and paths, and it is the ONLY correct source for them: the proxy's
#: generic hint (`readable/{env}/...`) is wrong for perf, which really lives at
#: `readable/spt/...`. Anything deriving a path from that hint gets perf wrong.
#:
#: Named here rather than reimplemented. A second fetcher was written before this
#: one was found, and it was worse in two ways that matter — no check that the key
#: matches the cert, and no check that the CN is scoped to the namespace the env
#: targets. Pointing at the good one beats maintaining a weaker copy.
FETCH_SCRIPT = "~/mcp-setup-bundle/fetch-temporal-cert.sh"


def _proxy_module():
    """Load the temporal proxy as a module so its ENV_MAP is the only ENV_MAP."""
    if not TEMPORAL_PROXY.exists():
        return None
    spec = importlib.util.spec_from_file_location("_temporal_proxy", TEMPORAL_PROXY)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cert_state(pem: Path, key: Path) -> tuple[str, str]:
    """(state, why) for one cert pair. state: ok | missing | empty | unreadable | expired."""
    if not pem.exists() or not key.exists():
        missing = [p.name for p in (pem, key) if not p.exists()]
        return "missing", f"no {' and no '.join(missing)}"
    # The bug this function exists for. An empty file passes every existence check
    # ever written and then fails inside TLS, where the error names PEM parsing
    # rather than the file that is empty.
    empty = [p.name for p in (pem, key) if p.stat().st_size == 0]
    if empty:
        verb = "is" if len(empty) == 1 else "are"
        return "empty", f"{' and '.join(empty)} {verb} 0 bytes — present but holds nothing"
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(pem.read_bytes())
    except Exception as exc:                                   # noqa: BLE001
        return "unreadable", f"{pem.name} will not parse as a certificate: {type(exc).__name__}"
    try:
        expires = cert.not_valid_after_utc.timestamp()
    except AttributeError:                                     # older cryptography
        expires = cert.not_valid_after.timestamp()
    days = (expires - time.time()) / 86400
    if days < 0:
        return "expired", f"expired {abs(int(days))} days ago"
    if days < EXPIRY_WARN_DAYS:
        return "expiring", f"expires in {int(days)} days"
    return "ok", f"valid for {int(days)} more days"


def certs() -> list[dict]:
    """Every Temporal env and whether its cert could actually be used.

    Local only — no network, no CLI. Cheap enough to run on every health pass,
    which matters: this is the check that turns a cryptic TLS error into a
    sentence naming the file.
    """
    mod = _proxy_module()
    if mod is None:
        return []
    config_dir = Path(getattr(mod, "CONFIG_DIR", ""))
    out = []
    for env, spec in getattr(mod, "ENV_MAP", {}).items():
        base = spec.get("cert", env)
        state, why = _cert_state(config_dir / f"{base}.pem", config_dir / f"{base}.key")
        out.append({"env": env, "cert": base, "state": state, "why": why,
                    "ok": state in ("ok", "expiring")})
    return out


#: States that mean somebody TRIED and it did not work. Distinct from "missing",
#: which for an env Arun never touches is a choice, not a fault — the same
#: unchecked-versus-broken line the Teams selector check had to learn. Reporting
#: four never-configured envs on every health pass is how a check gets ignored.
BROKEN_STATES = ("empty", "unreadable", "expired")


def broken_certs() -> list[dict]:
    """Certs that exist and cannot be used — a fault, not a gap."""
    return [c for c in certs() if c["state"] in BROKEN_STATES]


def unusable_envs() -> list[dict]:
    """Every env that would fail, including ones never configured."""
    return [c for c in certs() if not c["ok"]]


def summary() -> str:
    """One line for health, naming envs rather than counting them."""
    rows = certs()
    if not rows:
        return "temporal proxy not found — no envs to check"
    bad = [c for c in rows if not c["ok"]]
    soon = [c for c in rows if c["state"] == "expiring"]
    if not bad and not soon:
        return f"temporal certs: {len(rows)} envs, all usable"
    parts = [f"{c['env']} ({c['why']})" for c in bad + soon]
    return f"temporal certs: {len(rows) - len(bad)}/{len(rows)} usable — " + ", ".join(parts)


# --- reachability: the expensive half ----------------------------------------
#
# Measured on this machine, 2026-08-26: a Temporal list costs 5.0-14.2s and a
# Grafana label query 4.2s. That is the whole reason this is split from certs():
# a fifteen-second probe on every health pass would be paid all day to answer a
# question that changes about once a week.

#: Envs to probe. `prod` is deliberately absent — a health check has no business
#: opening connections to production on a timer, and sit shares its cluster, so a
#: nonprod probe already proves the network path and the CLI.
PROBE_ENV = "sit"

#: Past this, treat the surface as unusable rather than slow. The CLI's own gRPC
#: deadline trips around here and reports "context deadline exceeded", which
#: names the symptom and not the cause.
REACH_TIMEOUT = 30.0


async def temporal_reach(env: str = PROBE_ENV) -> dict:
    """Can Asta actually list workflows — and how long does it take."""
    mod = _proxy_module()
    if mod is None:
        return {"surface": "temporal", "ok": False, "why": "proxy not found", "seconds": 0.0}
    bad = [c for c in certs() if c["env"] == env and not c["ok"]]
    if bad:
        # Say the cert is the problem BEFORE spending 30s proving it over TLS.
        return {"surface": "temporal", "ok": False, "seconds": 0.0,
                "why": f"{env}: {bad[0]['why']}"}
    spec = getattr(mod, "ENV_MAP", {}).get(env)
    config_dir = Path(getattr(mod, "CONFIG_DIR", ""))
    base = spec["cert"]
    argv = [getattr(mod, "CLI", "temporal"), "workflow", "list",
            "--address", spec["address"], "--namespace", spec["namespace"], "--tls",
            "--tls-cert-path", str(config_dir / f"{base}.pem"),
            "--tls-key-path", str(config_dir / f"{base}.key"), "--limit", "1"]
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=REACH_TIMEOUT)
    except TimeoutError:
        return {"surface": "temporal", "ok": False, "seconds": REACH_TIMEOUT,
                "why": f"no answer in {REACH_TIMEOUT:.0f}s — VPN, or the cluster is slow"}
    except Exception as exc:                                   # noqa: BLE001
        return {"surface": "temporal", "ok": False, "seconds": 0.0,
                "why": f"{type(exc).__name__}: {str(exc)[:120]}"}
    secs = round(time.monotonic() - started, 1)
    if proc.returncode != 0:
        detail = (err or b"").decode()[:160] or f"exit {proc.returncode}"
        return {"surface": "temporal", "ok": False, "seconds": secs, "why": detail.strip()}
    return {"surface": "temporal", "ok": True, "seconds": secs,
            "why": f"listed workflows on {env} in {secs}s"}


async def run() -> dict:
    """Every debugging surface, with the cheap checks first."""
    cert_rows = certs()
    reach = await temporal_reach()
    return {"certs": cert_rows, "temporal": reach,
            "unusable": [c["env"] for c in cert_rows if not c["ok"]]}


def report(out: dict) -> str:
    """What Arun would want to read before trusting an answer from these tools."""
    lines = ["Debugging stack:"]
    rows = out.get("certs") or []
    usable = [c["env"] for c in rows if c["ok"]]
    lines.append(f"  temporal certs : {len(usable)}/{len(rows)} usable"
                 + (f" — {', '.join(usable)}" if usable else ""))
    for c in rows:
        if not c["ok"]:
            lines.append(f"      ✗ {c['env']}: {c['why']}")
    t = out.get("temporal") or {}
    lines.append(f"  temporal reach : {'ok' if t.get('ok') else '✗'} — {t.get('why', '?')}")
    return "\n".join(lines)


# --- the Temporal playbook, generated rather than maintained ------------------
#
# `skills/grafana-analyser.md` is a SYMLINK into the booking-service repo — that
# knowledge belongs to the team that owns the service. Temporal's mapping does
# not: it lives in a proxy on this laptop, so the playbook is generated here and
# written to a gitignored path.
#
# Generating it rather than committing a copy settles the drift question the only
# way that actually holds. A hand-written table is right on the day it is written;
# `_pins.yml` contradicting its own `lessons.md` is what the other outcome looks
# like. Regenerating is one call, and a test compares the file against ENV_MAP.

SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "temporal-analyser.md"

_SKILL_TEMPLATE = '---\nname: temporal-analyser\ndescription: >-\n  Investigate Temporal workflows — what a workflow did, why it is stuck, why it failed. Trigger\n  for: "why did the booking workflow fail", "is workflow X still running", "what happened to\n  ActivityPlanWorkflow", "which activity is retrying", stuck/pending/timed-out workflows, and\n  any question naming a workflow id, workflow type or task queue. Also the source of truth for\n  which Temporal namespace and client certificate an environment uses.\n---\n\n# Temporal workflow analysis\n\nUse the `temporal` MCP tools. Every call takes an `env`, and that alone selects the cluster,\nthe namespace and the mTLS certificate — never pass an address or namespace yourself.\n\n## Envs — namespace and certificate\n\nGenerated from the proxy\'s own `ENV_MAP`; `test_the_temporal_skill_matches_the_proxy` fails if\nthis table drifts from it. **Do not infer a namespace from an env name — several do not match.**\n\n| env | Temporal namespace | certificate | cluster |\n|---|---|---|---|\n{table}\nTwo traps worth stating plainly:\n\n- **The Temporal namespace is not the Grafana namespace.** Logs for sit live in\n  `telikos-sit`; Temporal for sit is `telikos-sit-cdt`. Using one where the other belongs\n  returns nothing, and nothing reads as "no workflows" rather than as a wrong lookup.\n- **`preprod` runs in `telikos-spt-cdt`**, not `telikos-preprod-cdt`. Guessing from the\n  pattern gives an answer that is wrong and looks right.\n\nCertificates are namespace-scoped (`CN=<namespace>:write`), so envs sharing a namespace share\none certificate file:\n{sharing}\n\n## Which tool, in order\n\n- `list_workflows` — **start here.** Returns id, type, status, start/close. Takes `query` in\n  Temporal list-filter syntax: `WorkflowType = "ActivityPlanWorkflow"`,\n  `ExecutionStatus = "Failed"`, `StartTime > "2026-08-01T00:00:00Z"`.\n- `count_workflows` — a bare total and nothing else. Only when a number is genuinely all that\n  is wanted; it cannot tell you *which*.\n- `describe_workflow` — one execution: status, timings, task queue, and **pending activities\n  with their failure**. This is where a stuck workflow explains itself.\n- `workflow_history` — **last resort.** Hundreds of events. `describe_workflow` already carries\n  the pending-activity failure, which is the answer most of the time.\n\n## Workflow\n\n1. `list_workflows`, filtered narrowly enough to be readable — status, type, or a time window.\n2. `describe_workflow` on the one that matters. Read **pending activities** first: a workflow\n   that looks hung is almost always one activity retrying against the same error.\n3. Only if that is genuinely not enough, `workflow_history`.\n\n## Correlating with logs\n\nA workflow id is an identifier like any other, so it traces in Loki as a line filter — see\n`grafana-analyser`. Temporal tells you *which activity failed and how often*; the logs tell you\n*why*. Use both: the retry count comes from Temporal, the exception from the logs.\n\n## Rules\n\n- Never guess a namespace or a cert path. The table above is the only source.\n- An empty result is not "no problem" — check the env is the one the work actually ran in.\n- 5-15s per call is normal for this cluster. `context deadline exceeded` is the CLI\'s own gRPC\n  deadline: it means slow or unreachable, **not** that the workflow does not exist.\n- If a cert is missing or empty the call fails inside TLS with a message about PEM parsing that\n  never mentions the file — run `debug_stack_health` before believing an empty result.\n- Always say which env you queried. An answer about sit presented as an answer about prod is\n  worse than no answer.\n'



def temporal_skill_text() -> str:
    """The playbook, with its env table taken straight from the proxy."""
    mod = _proxy_module()
    if mod is None:
        return ""
    env_map = mod.ENV_MAP
    rows = "\n".join(
        f"| `{env}` | `{spec['namespace']}` | `{spec['cert']}.pem` / `{spec['cert']}.key` | "
        f"{'prod' if 'prod-westeurope' in spec['address'] else 'nonprod'} |"
        for env, spec in env_map.items())
    shared: dict[str, list[str]] = {}
    for env, spec in env_map.items():
        shared.setdefault(spec["namespace"], []).append(env)
    sharing = "\n".join(
        f"- `{'` and `'.join(envs)}` share `{ns}`, and therefore share one certificate"
        for ns, envs in shared.items() if len(envs) > 1)
    return _SKILL_TEMPLATE.format(table=rows, sharing=sharing)


def write_temporal_skill() -> str:
    """Regenerate the playbook. Returns the path, or '' when there is no proxy."""
    text = temporal_skill_text()
    if not text:
        return ""
    SKILL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILL_PATH.write_text(text)
    return str(SKILL_PATH)
