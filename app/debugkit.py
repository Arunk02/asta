"""The tools behind debugging a case the way it was done for Vinish, 12 Oct.

Two TMS acks were "missing their Kafka consume logs". What found the answer was
not one wide query but a run of small ones, each built on the last:

  1. counts of an id per environment, service and pod — cheap, and the only way
     to know WHERE to look (Deepa's booking was in QA, not where it was guessed);
  2. the exact lines around one moment, from one logger — quoted, not paraphrased;
  3. a partial-id search when nothing matched ("H7QKN8NGNOR" was H7QKN8NGN9R);
  4. "is the log missing, or did it not happen?" — per-pod line counts against
     the log agent's dropped-lines metric (Prometheus): the pod was healthy and
     its lines were rejected by Loki (`ingester_error`).

Every query is built here, never by a brain: Loki's two-label rule, the right
namespace per environment, `app=` narrowing to stay under the byte cap, waiting
on a rate limit — the things that made 89 of 194 hand-written queries fail.
Every result carries UTC and local time, and a Grafana link a colleague can open.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import grafana

INSTANT_PATH = "loki/api/v1/query"
PROM_RANGE_PATH = "api/v1/query_range"
PROM_INSTANT_PATH = "api/v1/query"
#: Containers that are not the application: their lines are not JSON and not the story.
SIDECARS = "kuma-sidecar|istio-proxy|linkerd-proxy"


# --- small pieces ---------------------------------------------------------------------

def envs_for(env: str) -> list[str]:
    """'' or 'all' is every environment — never a guess at one."""
    e = (env or "").strip().lower()
    return grafana.envs() if e in ("", "all", "any", "*") else [e]


def namespace(env: str) -> str:
    return grafana.resolve_namespace(env)


def selector(ns: str, service: str = "", pod: str = "") -> str:
    parts = [f'namespace="{grafana.escape_label(ns)}"',
             grafana.cluster_selector(grafana.clusters()),
             f'container!~"{SIDECARS}"']
    if service:
        # "booking|activityplan" is two services, not one name with a pipe in it.
        alts = "|".join(grafana.escape_re2(x.strip()) for x in service.split("|") if x.strip())
        parts.append(f'app=~".*({alts}).*"')
    if pod:
        parts.append(f'pod="{grafana.escape_label(pod)}"')
    return "{" + ", ".join(parts) + "}"


def filters(terms) -> str:
    if isinstance(terms, str):
        terms = [terms]
    return "".join(f' |= "{grafana.escape_label(str(t))}"' for t in (terms or []) if t)


def parse_when(value: str) -> datetime | None:
    """An ISO time; no zone means UTC — the zone every log line here is written in."""
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def stamp(ts: float) -> str:
    """'09:50:03.962 UTC (15:20:03 local)' — both, every time: an IST time read as
    UTC put the first search for a missing line five and a half hours off."""
    utc = datetime.fromtimestamp(ts, timezone.utc)
    local = datetime.fromtimestamp(ts).astimezone()
    return f"{utc:%Y-%m-%d %H:%M:%S}.{int(utc.microsecond / 1000):03d} UTC ({local:%H:%M:%S} local)"


def explore_link(query: str, start: datetime, end: datetime, uid: str = "") -> str:
    """A Grafana Explore link for exactly this query and window. Deepa asked for
    'the log link' and was told one could not be made."""
    base = grafana.url().rstrip("/")
    if not base:
        return ""
    ds = uid or grafana.datasource_uid()
    pane = {"a": {"datasource": ds,
                  "queries": [{"refId": "A", "expr": query, "queryType": "range",
                               "datasource": {"type": "loki", "uid": ds}}],
                  "range": {"from": str(int(start.timestamp() * 1000)),
                            "to": str(int(end.timestamp() * 1000))}}}
    return f"{base}/explore?schemaVersion=1&orgId=1&panes={quote(json.dumps(pane, separators=(',', ':')))}"


async def _ask(path: str, params: dict, uid: str = "") -> dict:
    """One Grafana call; waits out a rate limit instead of giving up on it."""
    for attempt in range(4):
        try:
            return await grafana._get(path, params, uid)
        except grafana.GrafanaError as exc:
            if exc.status == 429 or "rate limit" in str(exc).lower():
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise
    raise grafana.GrafanaError("Grafana kept rate-limiting — tried four times", 429)


def _too_big(exc: Exception) -> bool:
    return "too many bytes" in str(exc) or "would read too many" in str(exc)


# --- 1. where is it? ------------------------------------------------------------------

async def counts(term: str = "", env: str = "", by: str = "app", minutes: int = 1440,
                 end: datetime | None = None, service: str = "") -> dict:
    """How many lines carry `term`, per env and per `by` (app or pod), over the
    window ending at `end`. A window too big for Loki is shortened, and the reply
    says to what — never silently."""
    end = end or datetime.now(timezone.utc)
    by = by if by in ("app", "pod", "container", "node_name") else "app"

    async def one(e: str) -> dict:
        ns = namespace(e)
        span = max(5, int(minutes))
        while True:
            q = (f"sum by ({by}) (count_over_time({selector(ns, service)}"
                 f"{filters(term)} [{span}m]))")
            try:
                data = await _ask(INSTANT_PATH, {"query": q, "time": grafana._ns(end)})
            except grafana.GrafanaError as exc:
                if _too_big(exc) and span > 30:
                    span //= 3
                    continue
                return {"env": e, "namespace": ns, "minutes": span, "error": str(exc)[:200]}
            rows = [{"key": (r.get("metric") or {}).get(by, "?"),
                     "count": int(float((r.get("value") or [0, 0])[1]))}
                    for r in (data.get("data") or {}).get("result") or []]
            rows.sort(key=lambda r: -r["count"])
            return {"env": e, "namespace": ns, "minutes": span, "rows": rows, "query": q}

    found = await asyncio.gather(*(one(e) for e in envs_for(env)))
    return {"term": term, "by": by, "end": end.timestamp(), "envs": list(found)}


def render_counts(found: dict) -> str:
    out = [f"Lines with “{found['term'] or '(anything)'}” by {found['by']}, window ending "
           f"{stamp(found['end'])}:"]
    for e in found["envs"]:
        head = f"- {e['env']} ({e['namespace']}, last {e['minutes'] // 60}h{e['minutes'] % 60:02d}m)"
        if e.get("error"):
            out.append(f"{head}: could not read — {e['error']}")
        elif not e.get("rows"):
            out.append(f"{head}: none")
        else:
            out.append(head + ": " + ", ".join(f"{r['key']} {r['count']}" for r in e["rows"][:8]))
    out.append("None in an environment means none in THAT window there — not that it never happened.")
    return "\n".join(out)


# --- 2. what exactly did it say? -----------------------------------------------------

def _row(rec: dict) -> dict:
    """A log record as a person reads it: when, which pod, which class, what."""
    labels = rec.get("labels") or {}
    line = rec.get("line") or ""
    msg, logger = line, ""
    if line.startswith("{"):
        try:
            body = json.loads(line)
            msg = str(body.get("message") or body.get("msg") or line)
            logger = str(body.get("logger") or body.get("logger_name") or "")
        except ValueError:
            pass
    return {"ts": rec["timestamp"], "pod": labels.get("pod", ""),
            "app": labels.get("app", ""), "logger": logger.rsplit(".", 1)[-1],
            "level": rec.get("level", ""), "message": msg}


async def lines(env: str, service: str = "", terms=(), logger: str = "",
                start: datetime | None = None, end: datetime | None = None,
                limit: int = 40, direction: str = "forward", pod: str = "") -> dict:
    """The exact lines — oldest first unless asked otherwise — and a link to them."""
    ns = namespace(env)
    end = end or datetime.now(timezone.utc)
    start = start or end - timedelta(minutes=30)
    q = selector(ns, service, pod) + filters(terms) + filters(logger)
    params = grafana.query_params(q, start, end, max(1, min(int(limit), 200)))
    params["direction"] = "backward" if direction == "backward" else "forward"
    try:
        records = grafana.parse(await _ask(grafana.QUERY_RANGE_PATH, params))
    except grafana.GrafanaError as exc:
        return {"env": env, "namespace": ns, "query": q, "error": str(exc)[:240],
                "link": explore_link(q, start, end), "rows": []}
    rows = [_row(r) for r in records]
    return {"env": env, "namespace": ns, "query": q, "rows": rows,
            "full": len(rows) >= int(limit), "start": start.timestamp(), "end": end.timestamp(),
            "link": explore_link(q, start, end)}


def render_lines(found: dict, width: int = 320) -> str:
    if found.get("error"):
        return f"Could not read {found['namespace']}: {found['error']}\nQuery: {found['query']}"
    if not found["rows"]:
        return (f"No lines in {found['namespace']} between {stamp(found['start'])} and "
                f"{stamp(found['end'])} for: {found['query']}\nLink: {found['link']}")
    out = [f"{len(found['rows'])} line(s) in {found['namespace']}"
           + (" — the limit was reached; narrow the window or add a filter for the rest"
              if found.get("full") else "") + ":"]
    for r in found["rows"]:
        pod = r["pod"].rsplit("-", 1)[-1] if r["pod"] else r["app"]
        out.append(f"  {stamp(r['ts'])} [{pod}] {r['logger'] or r['app']} :: "
                   f"{' '.join(r['message'].split())[:width]}")
    out.append(f"Link (Grafana Explore): {found['link']}")
    return "\n".join(out)


# --- 3. the id that was typed wrong ---------------------------------------------------

_CONFUSABLE = {"0": "O", "O": "0", "1": "I", "I": "1", "5": "S", "S": "5", "8": "B",
               "B": "8", "9": "O", "2": "Z", "Z": "2"}


def near(given: str, found: str) -> int:
    """Characters that differ, a look-alike swap (O/0, 9/O…) counting half."""
    if len(given) != len(found):
        return 99
    score = 0.0
    for a, b in zip(given.upper(), found.upper()):
        if a != b:
            score += 0.5 if _CONFUSABLE.get(a) == b or _CONFUSABLE.get(b) == a else 1
    return int(score * 2)


async def find_id(given: str, env: str = "", minutes: int = 1440,
                  service: str = "booking|activityplan") -> dict:
    """Ids that start like `given` — for when the exact one is nowhere. Ranked by
    how close they are to what was typed."""
    given = (given or "").strip().upper()
    prefix = given[:6]
    if len(prefix) < 5:
        return {"given": given, "error": "need at least the first five characters", "envs": []}
    pattern = re.compile(rf"\b{re.escape(prefix)}[A-Z0-9]{{{max(1, len(given) - 7)},{len(given) - 5}}}\b")

    async def one(e: str) -> dict:
        ns = namespace(e)
        end = datetime.now(timezone.utc)
        span = max(5, int(minutes))
        while True:
            q = selector(ns, service) + f' |= "{grafana.escape_label(prefix)}"'
            try:
                recs = grafana.parse(await _ask(
                    grafana.QUERY_RANGE_PATH,
                    grafana.query_params(q, end - timedelta(minutes=span), end, 500)))
            except grafana.GrafanaError as exc:
                if _too_big(exc) and span > 30:
                    span //= 3
                    continue
                return {"env": e, "error": str(exc)[:200], "matches": []}
            seen: dict[str, int] = {}
            for r in recs:
                for m in pattern.findall((r.get("line") or "").upper()):
                    seen[m] = seen.get(m, 0) + 1
            ranked = sorted(seen.items(), key=lambda kv: (near(given, kv[0]), -kv[1]))
            return {"env": e, "minutes": span,
                    "matches": [{"id": k, "lines": v, "distance": near(given, k)}
                                for k, v in ranked[:6]]}

    return {"given": given, "envs": list(await asyncio.gather(*(one(e) for e in envs_for(env))))}


def render_find(found: dict) -> str:
    if found.get("error"):
        return found["error"]
    out = [f"Ids starting like {found['given']}:"]
    for e in found["envs"]:
        if e.get("error"):
            out.append(f"- {e['env']}: could not read — {e['error']}")
        elif not e["matches"]:
            out.append(f"- {e['env']}: none")
        else:
            out.append(f"- {e['env']}: " + ", ".join(
                f"{m['id']} ({m['lines']} lines{', exact' if m['distance'] == 0 else ''})"
                for m in e["matches"]))
    return "\n".join(out)


# --- 4. missing log, or missing event? ------------------------------------------------

async def _prom(expr: str, start: datetime, end: datetime, step: int) -> list[dict]:
    data = await _ask(PROM_RANGE_PATH, {"query": expr, "start": str(int(start.timestamp())),
                                        "end": str(int(end.timestamp())), "step": str(step)},
                      uid=grafana.prometheus_uid())
    return (data.get("data") or {}).get("result") or []


async def gaps(env: str, service: str, start: datetime, end: datetime) -> dict:
    """Per pod, per minute: lines that reached Loki, lines the node's log agent
    dropped and why, restarts and CPU throttling. A pod that is healthy and busy
    but silent while its node drops lines lost its LOGS, not its work."""
    ns = namespace(env)
    sel = selector(ns, service)
    q = f"sum by (pod, node_name) (count_over_time({sel} [1m]))"
    params = {"query": q, "start": grafana._ns(start), "end": grafana._ns(end), "step": "60"}
    result = (await _ask(grafana.QUERY_RANGE_PATH, params)).get("data", {}).get("result") or []
    pods: dict[str, dict] = {}
    for r in result:
        m = r.get("metric") or {}
        pods[m.get("pod", "?")] = {"node": m.get("node_name", ""),
                                   "lines": {int(float(t)): int(float(v)) for t, v in r.get("values") or []}}
    # A minute with no lines is ABSENT from Loki's answer, not zero — and those
    # are exactly the minutes that matter (vm5rh, 09:51–09:52 on 12 Oct).
    first = int(start.timestamp()) // 60 * 60 + 60
    for info in pods.values():
        for t in range(first, int(end.timestamp()) + 1, 60):
            info["lines"].setdefault(t, 0)
    nodes = sorted({p["node"] for p in pods.values() if p["node"]})
    cluster = grafana.cluster_selector(grafana.clusters())
    dropped: dict[str, dict] = {}
    health: dict[str, dict] = {}
    notes = []
    if nodes:
        node_re = "|".join(grafana.escape_re2(n) for n in nodes)
        try:
            for r in await _prom(
                    f'sum by (node_name, reason) (increase(loki_write_dropped_entries_total'
                    f'{{{cluster}, node_name=~"{node_re}"}}[1m]))', start, end, 60):
                m = r.get("metric") or {}
                per = dropped.setdefault(m.get("node_name", "?"), {})
                for t, v in r.get("values") or []:
                    if float(v) >= 1:
                        key = int(float(t))
                        per.setdefault(key, {})[m.get("reason", "?")] = int(float(v))
        except grafana.GrafanaError as exc:
            notes.append(f"log-agent drop metric unreadable: {str(exc)[:160]}")
    pod_re = "|".join(grafana.escape_re2(p) for p in pods)
    if pod_re:
        for name, expr in (
                ("restarts", f'max by (pod) (increase(kube_pod_container_status_restarts_total'
                             f'{{namespace="{ns}", pod=~"{pod_re}"}}[1m]))'),
                ("throttled", f'max by (pod) (rate(container_cpu_cfs_throttled_periods_total'
                              f'{{namespace="{ns}", pod=~"{pod_re}", container!~"{SIDECARS}"}}[1m]) '
                              f'/ rate(container_cpu_cfs_periods_total{{namespace="{ns}", '
                              f'pod=~"{pod_re}", container!~"{SIDECARS}"}}[1m]))')):
            try:
                for r in await _prom(expr, start, end, 60):
                    pod = (r.get("metric") or {}).get("pod", "?")
                    vals = [float(v) for _, v in r.get("values") or [] if v not in ("NaN",)]
                    health.setdefault(pod, {})[name] = max(vals) if vals else 0.0
            except grafana.GrafanaError as exc:
                notes.append(f"{name} unreadable: {str(exc)[:120]}")
    return {"env": env, "namespace": ns, "service": service, "start": start.timestamp(),
            "end": end.timestamp(), "pods": pods, "dropped": dropped, "health": health,
            "notes": notes}


def silent_minutes(series: dict[int, int]) -> list[int]:
    """Minutes a pod logged under a quarter of its own usual rate."""
    if not series:
        return []
    typical = statistics.median(series.values()) or 0
    return [t for t, v in sorted(series.items()) if typical >= 50 and v < typical / 4]


def render_gaps(found: dict) -> str:
    out = [f"Pods of {found['service']} in {found['namespace']}, "
           f"{stamp(found['start'])} → {stamp(found['end'])}:"]
    verdicts = []
    for pod, info in sorted(found["pods"].items()):
        quiet = silent_minutes(info["lines"])
        drops = found["dropped"].get(info["node"], {})
        h = found["health"].get(pod, {})
        lost = sum(sum(r.values()) for r in drops.values())
        reasons = sorted({k for r in drops.values() for k in r})
        out.append(f"- {pod} on {info['node']}: median {int(statistics.median(info['lines'].values() or [0]))}"
                   f" lines/min; quiet minutes: "
                   + (", ".join(datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M") for t in quiet) or "none")
                   + f"; node dropped {lost} lines" + (f" ({', '.join(reasons)})" if reasons else "")
                   + f"; restarts {int(h.get('restarts', 0))}; max CPU throttling {h.get('throttled', 0):.0%}")
        if quiet and lost and not h.get("restarts"):
            verdicts.append(f"{pod} kept running but its logs did not all arrive: quiet at "
                            + ", ".join(datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M") for t in quiet[:6])
                            + f" UTC while its node's log agent dropped {lost} lines"
                            + (f" ({', '.join(reasons)})" if reasons else "")
                            + ". A line missing from this pod then is a lost log, not a missed event.")
    out += found["notes"]
    out += verdicts or ["No pod went quiet against its own rate in this window."]
    return "\n".join(out)
