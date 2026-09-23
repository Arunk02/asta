"""Grafana/Loki through its own API — code writes the query, the model reads an answer.

Measured on 22 Sep, over three weeks of the brains' own session logs: **194 Grafana
MCP calls, 89 of them failed** — and not one was a login or a connection problem.
56 came back bigger than a model can read, 13 were rejected for too few label
matchers, 4 blew the 200 GiB scan limit, 8 were "only label matchers supported".
Handed raw LogQL and 43 tool definitions, the model becomes the query engine, and
it writes queries Loki refuses.

So the query is built here, from arguments, and the result is summarised here:

  * the selector always carries namespace AND cluster, because Loki enforces a
    two-label minimum and a one-label query is the 13 rejections;
  * search terms become `|=` filters, so Loki narrows rather than us;
  * what comes back is bucketed into error signatures — the dominant one, how
    often, first and last seen, stack frames, trace ids — which is what an
    investigation actually needs and is a hundredth of the size.

The contract itself (endpoint, nanosecond timestamps, two-label rule, RE2
escaping that must not escape a hyphen) is ported from his incident resolver,
which built it against the real cluster. Nothing here imports that repo: it is a
copy of the contract, and the values it needs live in his .env, never in this
public repository.

The Grafana MCP server stays as the fallback: when the API path fails, the same
LogQL goes to the MCP server and the same records come back. Primary and backup,
rather than one flaky path.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

TIMEOUT = 60.0


# --- what this machine is configured to talk to -------------------------------------

def url() -> str:
    return (os.environ.get("ASTA_GRAFANA_URL") or "").strip()


def datasource_uid() -> str:
    return (os.environ.get("ASTA_GRAFANA_DATASOURCE_UID") or "loki").strip()


def namespace() -> str:
    """The Loki namespace to read when he names no environment."""
    return (os.environ.get("ASTA_GRAFANA_NAMESPACE") or "").strip()


def clusters() -> list[str]:
    named = os.environ.get("ASTA_GRAFANA_CLUSTERS") or ""
    return [c.strip() for c in named.split(",") if c.strip()]


def excluded_apps() -> str:
    """Self-monitoring apps whose own logs drown out everything else."""
    return (os.environ.get("ASTA_GRAFANA_EXCLUDED_APPS") or "").strip()


def max_lines() -> int:
    try:
        return max(1, int((os.environ.get("ASTA_GRAFANA_MAX_LINES") or "500").strip()))
    except ValueError:
        return 500


def window_minutes() -> int:
    try:
        return max(1, int((os.environ.get("ASTA_GRAFANA_WINDOW_MINUTES") or "30").strip()))
    except ValueError:
        return 30


def enabled() -> bool:
    switch = (os.environ.get("ASTA_GRAFANA") or "1").strip()
    return bool(url() and datasource_uid() and switch not in ("0", "off"))


class GrafanaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --- the query -----------------------------------------------------------------------

#: LogQL/RE2 metacharacters. A hyphen is deliberately NOT escaped: `re.escape`
#: emits `\-`, which RE2 rejects outside a character class.
_RE2_SPECIAL = re.compile(r"([.+*?()\[\]{}^$|/\\])")

QUERY_RANGE_PATH = "loki/api/v1/query_range"
LABEL_VALUES_PATH = "loki/api/v1/label/{label}/values"

#: Levels that are never worth an incident's attention.
_NOISE_LEVELS = ("debug", "info", "information", "trace", "verbose")
ERROR_TOKENS = r"(?i)\\b(error|exception|fatal|panic|traceback)\\b"


def escape_re2(value: str) -> str:
    return _RE2_SPECIAL.sub(r"\\\1", value)


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def cluster_selector(names: list[str]) -> str:
    if not names:
        return 'k8s_cluster=~".+"'
    if len(names) == 1:
        return f'k8s_cluster="{escape_label(names[0])}"'
    return 'k8s_cluster=~"' + "|".join(escape_label(c) for c in names) + '"'


def build_selector(ns: str, names: list[str], excluded: str, service: str | None) -> str:
    """A stream selector that honours Loki's two-label minimum."""
    parts = [f'namespace="{escape_label(ns)}"', cluster_selector(names)]
    if excluded:
        parts.append(f'app!~"{excluded}"')
    if service:
        parts.append(f'app=~".*{escape_re2(service)}.*"')
    return "{" + ", ".join(parts) + "}"


def build_query(ns: str, *, service: str | None = None, terms: list[str] | None = None,
                errors_only: bool = True, names: list[str] | None = None,
                excluded: str | None = None) -> str:
    """A TARGETED query: never a broad unbounded pull."""
    query = build_selector(ns, names if names is not None else clusters(),
                           excluded_apps() if excluded is None else excluded, service)
    for term in terms or []:
        if term:
            query += f' |= "{escape_label(str(term))}"'
    if errors_only:
        query += f' |~ "{ERROR_TOKENS}"'
        query += f' | level!~"(?i)^({"|".join(_NOISE_LEVELS)})$"'
    return query


def _ns(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1e9))


def query_params(query: str, start: datetime, end: datetime, limit: int) -> dict[str, str]:
    return {"query": query, "start": _ns(start), "end": _ns(end),
            "limit": str(limit), "direction": "backward"}


def _proxy(path: str) -> str:
    return f"{url().rstrip('/')}/api/datasources/proxy/uid/{datasource_uid()}/{path}"


# --- the token ------------------------------------------------------------------------
#
# Grafana service-account tokens minted through the key service expire in about half an
# hour, so a 401 means "mint a new one and try once more", not "access denied".

_token: dict[str, Any] = {"value": "", "at": 0.0}


def _standing_token() -> str:
    """A token he pasted in himself, if any — minting is the normal path."""
    return (os.environ.get("ASTA_GRAFANA_TOKEN") or "").strip()


async def _mint(client: httpx.AsyncClient) -> str:
    """A fresh Grafana key, through Azure AD and the key service. '' when it cannot."""
    tenant = (os.environ.get("ARM_TENANT_ID") or "").strip()
    cid = (os.environ.get("ARM_CLIENT_ID") or "").strip()
    secret = (os.environ.get("ARM_CLIENT_SECRET") or "").strip()
    if not (tenant and cid and secret):
        return ""
    token_url = (os.environ.get("ASTA_GRAFANA_TOKEN_URL") or "").strip()
    if not token_url:
        return ""
    resp = await client.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": cid,
              "client_secret": secret, "scope": "https://management.azure.com/.default"},
        headers={"content-type": "application/x-www-form-urlencoded"})
    if resp.status_code >= 400:
        # Azure puts the actionable part in the body (AADSTS7000215 = bad secret);
        # the status alone cannot be acted on.
        raise GrafanaError(f"Azure AD refused the service principal: "
                           f"{resp.status_code} {resp.text[:200]}")
    bearer = (resp.json() or {}).get("access_token", "")
    if not bearer:
        raise GrafanaError("Azure AD returned no access token")
    key = await client.post(token_url, headers={"Authorization": f"Bearer {bearer}"})
    if key.status_code >= 400:
        raise GrafanaError(f"the Grafana key service refused: {key.status_code} {key.text[:200]}")
    data = key.json() or {}
    token = (data.get("key") or data.get("token") or data.get("apiKey")
             or data.get("grafanaToken") or (data.get("data") or {}).get("key") or "")
    if not token:
        raise GrafanaError("the Grafana key service returned no key")
    _token.update(value=token, at=time.time())
    return token


async def _get(path: str, params: dict[str, str]) -> dict:
    if not enabled():
        raise GrafanaError("Grafana is not configured — ASTA_GRAFANA_URL and "
                           "ASTA_GRAFANA_DATASOURCE_UID are what it needs")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        token = _token["value"] or _standing_token() or await _mint(client)
        if not token:
            raise GrafanaError("no Grafana token, and no service principal to mint one "
                               "(ARM_CLIENT_ID / ARM_CLIENT_SECRET / ARM_TENANT_ID)")
        head = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        resp = await client.get(_proxy(path), params=params, headers=head)
        if resp.status_code == 401:                       # expired: mint once, retry once
            token = await _mint(client)
            if token:
                head["Authorization"] = f"Bearer {token}"
                resp = await client.get(_proxy(path), params=params, headers=head)
        if resp.status_code >= 400:
            detail = " ".join(resp.text.split())[:200]
            raise GrafanaError(f"Grafana said {resp.status_code}: {detail}", resp.status_code)
        return resp.json() or {}


# --- reading what came back -----------------------------------------------------------

EXCEPTION_CLASS = re.compile(r"\b((?:[a-z][\w$]*\.){1,8}[A-Z][\w$]*(?:Exception|Error|Throwable))\b")
BARE_EXCEPTION = re.compile(r"\b([A-Z][A-Za-z0-9_$]{2,}(?:Exception|Error))\b")
STACK_FRAME = re.compile(r"\bat\s+((?:[\w$]+\.)+[\w$<>]+)\(([\w$]+\.(?:java|kt)):(\d+)\)")
LEVEL = re.compile(r"(?i)\b(ERROR|WARN|INFO|DEBUG|FATAL)\b")
TRACE_IDS: tuple[re.Pattern[str], ...] = (
    re.compile(r'"[Tt]race[_\-]?[Ii][Dd]"\s*:\s*"([0-9a-fA-F\-]{16,})"'),
    re.compile(r'[Tt]race[_\-]?[Ii][Dd]["\s:=,\]]+([0-9a-fA-F\-]{16,})'),
    re.compile(r"traceparent[=: ]+\d{2}-([0-9a-fA-F]{32})-"),
    re.compile(r"\b([0-9a-fA-F]{32})\b"),
)
_BAD_LEVELS = ("error", "fatal", "panic", "severe", "critical", "err", "warn", "warning")


def detect_level(line: str, labels: dict[str, str]) -> str:
    level = (labels.get("level") or labels.get("detected_level") or "").lower()
    if level:
        return level
    found = LEVEL.search(line)
    if found:
        return found.group(1).lower()
    if '"level"' in line:
        try:
            return str((json.loads(line) or {}).get("level", "")).lower() or "info"
        except (ValueError, TypeError):
            pass
    return "info"


def trace_id(line: str, labels: dict[str, str]) -> str:
    for key in ("traceId", "trace_id", "traceID", "traceid", "trace"):
        if labels.get(key):
            return str(labels[key]).replace("-", "")
    for pattern in TRACE_IDS:
        found = pattern.search(line)
        if found:
            return found.group(1).replace("-", "")
    return ""


def exception_class(line: str) -> str:
    found = EXCEPTION_CLASS.search(line)
    if found:
        return found.group(1)
    bare = BARE_EXCEPTION.search(line)
    return bare.group(1) if bare else ""


def stack_frames(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        for f in STACK_FRAME.finditer(line):
            out.append({"method": f.group(1), "file": f.group(2), "line": int(f.group(3))})
    return out


def _json_body(line: str) -> dict:
    """The JSON object a structured log line carries, or {}."""
    start = line.find("{")
    if start < 0 or "}" not in line:
        return {}
    try:
        found = json.loads(line[start:])
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def signature(line: str) -> str:
    """A stable name for an error, so two of the same defect count as one.

    Structured logs are why this is not just "normalise the line": a JSON line
    carries a timestamp, a thread name, a trace id and a span id before it gets
    anywhere near the message, and normalising the whole blob made 500 lines into
    361 "distinct" errors — which is a log dump wearing a summary's clothes.
    """
    exc, frames = exception_class(line), stack_frames([line])
    if exc and frames:
        return f"{exc} @ {frames[0]['method'].rsplit('.', 1)[-1]}():{frames[0]['line']}"
    if exc:
        return exc
    body = _json_body(line)
    if body:
        said = str(body.get("message") or body.get("msg") or body.get("error")
                   or body.get("exception") or "")
        logger = str(body.get("logger") or body.get("loggerName") or body.get("class") or "")
        if said:
            line = (f"{logger.rsplit('.', 1)[-1]}: {said}" if logger else said)
    flat = re.sub(r"[0-9a-fA-F]{8,}", "*", line)
    flat = re.sub(r"\d+", "N", flat)
    # The payload is what varies between two of the SAME error: one request's
    # uri, another's ids. Keeping it split 500 lines into 362 "distinct" errors.
    flat = re.sub(r"\([^)]{10,}\)", "(…)", flat)
    flat = re.sub(r'"[^"]{10,}"', '"…"', flat)
    flat = re.sub(r"\[[^\]]{10,}\]", "[…]", flat)
    return re.sub(r"\s+", " ", flat).strip()[:80]


def parse(payload: dict) -> list[dict]:
    """Loki's streams and values, flattened into records, oldest first."""
    records: list[dict] = []
    for stream in (payload.get("data") or {}).get("result") or []:
        labels = {str(k): str(v) for k, v in (stream.get("stream") or {}).items()}
        service = (labels.get("app") or labels.get("container")
                   or labels.get("pod", "unknown").rsplit("-", 2)[0])
        for entry in stream.get("values") or []:
            if len(entry) < 2:
                continue
            try:
                when = int(entry[0]) / 1e9
            except (TypeError, ValueError):
                continue
            records.append({"timestamp": when, "line": entry[1], "labels": labels,
                            "service": service, "level": detect_level(entry[1], labels),
                            "trace_id": trace_id(entry[1], labels)})
    records.sort(key=lambda r: r["timestamp"])
    return records


def summarise(records: list[dict]) -> dict:
    """Group before anyone reads it: this is the difference between an answer and
    56 responses too large to read."""
    buckets: dict[str, dict] = {}
    for record in records:
        if record.get("level") not in _BAD_LEVELS:
            continue
        key = signature(record["line"])
        bucket = buckets.setdefault(key, {
            "signature": key, "count": 0, "service": record.get("service", ""),
            "first_seen": record["timestamp"], "last_seen": record["timestamp"],
            "level": record["level"], "samples": [], "trace_ids": [],
            "exception": exception_class(record["line"])})
        bucket["count"] += 1
        bucket["first_seen"] = min(bucket["first_seen"], record["timestamp"])
        bucket["last_seen"] = max(bucket["last_seen"], record["timestamp"])
        if len(bucket["samples"]) < 4:
            bucket["samples"].append(record["line"][:400])
        if record["trace_id"] and record["trace_id"] not in bucket["trace_ids"]:
            bucket["trace_ids"].append(record["trace_id"])
    ordered = sorted(buckets.values(), key=lambda b: (b["count"], -b["first_seen"]), reverse=True)
    frames = stack_frames([r["line"] for r in records])
    # A stack trace spans several lines, so the exception and its top frame land in
    # different buckets. Recombined, the dominant signature says where it happened.
    if ordered and frames and ordered[0].get("exception") and "@" not in ordered[0]["signature"]:
        top = frames[0]
        ordered[0]["signature"] = (f"{ordered[0]['exception']} @ "
                                   f"{str(top['method']).rsplit('.', 1)[-1]}():{top['line']}")
        ordered[0]["top_frame"] = top
    return {"scanned": len(records), "signatures": ordered,
            "dominant": ordered[0] if ordered else None, "stack_frames": frames[:20]}


# --- what a brain (or he) actually calls ----------------------------------------------

async def logs(service: str = "", terms: list[str] | None = None, minutes: int = 0,
               ns: str = "", errors_only: bool = True, limit: int = 0,
               end: datetime | None = None) -> dict:
    """Targeted Loki search, summarised. Raises GrafanaError with a usable reason."""
    ns = ns or namespace()
    if not ns:
        raise GrafanaError("no namespace to search — ASTA_GRAFANA_NAMESPACE, or say which")
    minutes = minutes or window_minutes()
    limit = limit or max_lines()
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    query = build_query(ns, service=service or None, terms=list(terms or []),
                        errors_only=errors_only)
    began = time.monotonic()
    fallback = ""
    try:
        records = parse(await _get(QUERY_RANGE_PATH, query_params(query, start, end, limit)))
    except GrafanaError as exc:
        records, fallback = await _via_mcp(query, start, end, limit, exc)
    out = summarise(records)
    out.update(query=query, namespace=ns, service=service, minutes=minutes,
               took_ms=int((time.monotonic() - began) * 1000),
               via="mcp fallback" if fallback else "api", fallback_reason=fallback)
    return out


def render(found: dict, lines: int = 6) -> str:
    """The summary as a brain reads it — signatures, not a log dump."""
    where = f"{found.get('namespace', '')}"
    if found.get("service"):
        where += f" · {found['service']}"
    head = (f"Loki {where}, last {found.get('minutes', 0)} min: "
            f"{found.get('scanned', 0)} matching lines")
    if found.get("via") == "mcp fallback":
        head += " (via the MCP fallback — the API path failed: "
        head += f"{found.get('fallback_reason', '')[:120]})"
    if not found.get("signatures"):
        return head + ". No errors in that window."
    out = [head + f", {len(found['signatures'])} distinct error signature(s):"]
    for sig in found["signatures"][:lines]:
        first = time.strftime("%H:%M:%S", time.localtime(sig["first_seen"]))
        last = time.strftime("%H:%M:%S", time.localtime(sig["last_seen"]))
        out.append(f"  ×{sig['count']} [{sig['level']}] {sig['service']}: "
                   f"{sig['signature'][:160]}  ({first}→{last})")
        if sig.get("trace_ids"):
            out.append(f"      traces: {', '.join(sig['trace_ids'][:3])}")
    if len(found["signatures"]) > lines:
        out.append(f"  …and {len(found['signatures']) - lines} more signatures")
    top = found.get("dominant") or {}
    if top.get("samples"):
        out.append("  first sample: " + top["samples"][0][:300])
    if found.get("stack_frames"):
        frames = ", ".join(f"{f['method']}({f['file']}:{f['line']})"
                           for f in found["stack_frames"][:3])
        out.append("  top frames: " + frames)
    return "\n".join(out)


async def namespaces() -> list[str]:
    """Every namespace Loki knows — the answer to "which spelling is the real one"."""
    payload = await _get(LABEL_VALUES_PATH.format(label="namespace"), {})
    return [str(v) for v in (payload.get("data") or [])]


async def health() -> tuple[bool, str]:
    if not enabled():
        return False, "not configured (ASTA_GRAFANA_URL / ASTA_GRAFANA_DATASOURCE_UID)"
    try:
        found = await namespaces()
    except GrafanaError as exc:
        return False, str(exc)[:200]
    return True, f"{len(found)} namespaces on datasource {datasource_uid()}"


# --- the backup path ------------------------------------------------------------------
#
# Same LogQL, same records, through the Grafana MCP server. Used only when the API
# path fails: MCP is the fallback now, not the road.

MCP_PROTOCOL = "2024-11-05"


def mcp_enabled() -> bool:
    switch = (os.environ.get("ASTA_GRAFANA_MCP") or "1").strip()
    return bool((os.environ.get("ASTA_GRAFANA_MCP_URL") or "").strip()) and switch not in ("0", "off")


async def _via_mcp(query: str, start: datetime, end: datetime, limit: int,
                   why: GrafanaError) -> tuple[list[dict], str]:
    """(records, reason the API path failed). Re-raises the ORIGINAL error when the
    fallback cannot answer either — the first failure is the one worth reporting."""
    if not mcp_enabled():
        raise why
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            token = await _mcp_token(client)
            _, session = await _mcp_rpc(client, token, None, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                           "clientInfo": {"name": "asta", "version": "1.0.0"}}})
            result, _ = await _mcp_rpc(client, token, session, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "query_loki_logs", "arguments": {
                    "datasourceUid": datasource_uid(), "logql": query,
                    "startRfc3339": start.isoformat(), "endRfc3339": end.isoformat(),
                    "limit": limit, "env": _mcp_env()}}})
        return _mcp_records(result), str(why)
    except Exception as exc:                               # noqa: BLE001
        raise GrafanaError(f"{why} — and the MCP fallback failed too: "
                           f"{str(exc)[:160]}") from exc


def _mcp_env() -> str:
    """The MCP server's own env name, from the namespace ("team-prod" → "prod")."""
    named = (os.environ.get("ASTA_GRAFANA_MCP_ENV") or "").strip()
    if named:
        return named
    ns = namespace()
    return ns.split("-", 1)[1] if "-" in ns else "dev"


async def _mcp_token(client: httpx.AsyncClient) -> str:
    tenant = (os.environ.get("ARM_TENANT_ID") or "").strip()
    cid = (os.environ.get("ARM_CLIENT_ID") or "").strip()
    secret = (os.environ.get("ARM_CLIENT_SECRET") or "").strip()
    scope = (os.environ.get("ASTA_GRAFANA_MCP_SCOPE") or "").strip()
    if not (tenant and cid and secret and scope):
        raise GrafanaError("the MCP fallback needs the service principal and a scope")
    resp = await client.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": cid,
              "client_secret": secret, "scope": scope},
        headers={"content-type": "application/x-www-form-urlencoded"})
    if resp.status_code >= 400:
        raise GrafanaError(f"Azure AD refused: {resp.status_code} {resp.text[:160]}")
    token = (resp.json() or {}).get("access_token", "")
    if not token:
        raise GrafanaError("Azure AD returned no access token for the MCP scope")
    return token


async def _mcp_rpc(client: httpx.AsyncClient, token: str, session: str | None,
                   payload: dict) -> tuple[dict, str | None]:
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}"}
    if session:
        headers["Mcp-Session-Id"] = session
    resp = await client.post((os.environ.get("ASTA_GRAFANA_MCP_URL") or "").strip(), json=payload, headers=headers)
    if resp.status_code >= 400:
        raise GrafanaError(f"the MCP server said {resp.status_code}: {resp.text[:160]}")
    session = resp.headers.get("Mcp-Session-Id") or session
    if resp.headers.get("Content-Type", "").startswith("text/event-stream"):
        data: dict = {}
        for line in resp.text.splitlines():
            if line.startswith("data:") and line[5:].strip():
                data = json.loads(line[5:].strip())
        return data, session
    return resp.json() or {}, session


def _mcp_records(result: dict) -> list[dict]:
    if result.get("error"):
        raise GrafanaError(f"MCP error: {str(result['error'])[:160]}")
    call = result.get("result") or {}
    text = next((b.get("text", "") for b in (call.get("content") or [])
                 if b.get("type") == "text"), "")
    if not text:
        return []
    if call.get("isError"):
        raise GrafanaError(f"MCP tool error: {text[:160]}")
    body = json.loads(text)
    records = []
    for entry in body.get("data") or []:
        try:
            when = int(str(entry.get("timestamp", "")).strip('"')) / 1e9
        except (TypeError, ValueError):
            continue
        labels = {str(k): str(v) for k, v in (entry.get("labels") or {}).items()}
        line = entry.get("line", "")
        records.append({"timestamp": when, "line": line, "labels": labels,
                        "service": (labels.get("app") or labels.get("container")
                                    or labels.get("pod", "unknown").rsplit("-", 2)[0]),
                        "level": detect_level(line, labels),
                        "trace_id": trace_id(line, labels)})
    records.sort(key=lambda r: r["timestamp"])
    return records
