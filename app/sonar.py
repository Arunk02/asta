"""The SonarQube quality gate, read from Sonar itself.

A red quality gate says nothing in the CI log but "Quality Gate has FAILED" and a
dashboard link: which condition failed, and on what code, lives on the Sonar
server. 5 Nov, #521: without it the automatic fix round wrote tests twice for a
gate that was not failing on coverage, and the PR stayed red.

With `ASTA_SONAR_TOKEN` (a user token Arun creates on Sonar: My Account →
Security → Generate Token) this reads the gate's failing conditions, the open
issues on the change, and the files with uncovered new lines — the brief the fix
round plans from. Without it, nothing is guessed: Asta says what it needs. The
token is sent only to the Sonar host, as HTTP basic auth, and never logged.
"""
from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

#: "Detailed information can be found at: https://<host>/dashboard?id=<project>&pullRequest=<n>"
_DASHBOARD = re.compile(r"https?://[^\s\"'<>]+/dashboard\?[^\s\"'<>]+")


def configured() -> bool:
    return bool((os.environ.get("ASTA_SONAR_TOKEN") or "").strip())


def target(ci_read: str) -> dict:
    """{host, project, pullRequest | branch} from the dashboard link a CI log prints."""
    for m in reversed(_DASHBOARD.findall(ci_read or "")):
        u = urlparse(m)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if q.get("id"):
            host = (os.environ.get("ASTA_SONAR_URL") or f"{u.scheme}://{u.netloc}").rstrip("/")
            out = {"host": host, "project": q["id"]}
            if q.get("pullRequest"):
                out["pullRequest"] = q["pullRequest"]
            elif q.get("branch"):
                out["branch"] = q["branch"]
            return out
    return {}


def _scope(t: dict) -> dict:
    return {k: t[k] for k in ("pullRequest", "branch") if t.get(k)}


async def report(t: dict, client=None) -> dict:
    """The gate's failing conditions, the change's open issues and uncovered files."""
    import httpx
    token = (os.environ.get("ASTA_SONAR_TOKEN") or "").strip()
    if not token or not t.get("host") or not t.get("project"):
        return {}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, auth=(token, ""))
    try:
        scope = _scope(t)
        gate = (await client.get(f"{t['host']}/api/qualitygates/project_status",
                                 params={"projectKey": t["project"], **scope})).json()
        status = gate.get("projectStatus") or {}
        failed = [c for c in status.get("conditions") or [] if c.get("status") == "ERROR"]
        issues = (await client.get(f"{t['host']}/api/issues/search",
                                   params={"componentKeys": t["project"], "resolved": "false",
                                           "ps": 50, **scope})).json().get("issues") or []
        uncovered = []
        if any("coverage" in (c.get("metricKey") or "") or "uncovered" in (c.get("metricKey") or "")
               for c in failed):
            tree = (await client.get(f"{t['host']}/api/measures/component_tree",
                                     params={"component": t["project"], "qualifiers": "FIL",
                                             "metricKeys": "new_uncovered_lines,new_coverage",
                                             "ps": 100, **scope})).json()
            for comp in tree.get("components") or []:
                vals = {m["metric"]: (m.get("period") or {}).get("value", m.get("value"))
                        for m in comp.get("measures") or []}
                if float(vals.get("new_uncovered_lines") or 0) > 0:
                    uncovered.append({"file": comp.get("path") or comp.get("key"),
                                      "uncovered": vals.get("new_uncovered_lines"),
                                      "coverage": vals.get("new_coverage")})
        return {"status": status.get("status", ""), "failed": failed,
                "issues": issues, "uncovered": uncovered}
    finally:
        if own:
            await client.aclose()


def render(rep: dict) -> str:
    """The report as a brief: what failed, and on which lines."""
    if not rep:
        return ""
    lines = [f"SonarQube quality gate: {rep.get('status') or 'unknown'}. Failing conditions:"]
    for c in rep.get("failed") or []:
        lines.append(f"- {c.get('metricKey')}: {c.get('actualValue')} "
                     f"(must be {c.get('comparator', '')} {c.get('errorThreshold')})")
    if not rep.get("failed"):
        lines.append("- none reported as ERROR")
    if rep.get("issues"):
        lines.append("Open issues on this change:")
        for i in rep["issues"][:20]:
            where = (i.get("component") or "").split(":", 1)[-1]
            lines.append(f"- {i.get('severity', '')} {i.get('rule', '')} {where}:{i.get('line', '?')} "
                         f"— {i.get('message', '')}")
    if rep.get("uncovered"):
        lines.append("New lines not covered by tests:")
        for u in rep["uncovered"][:15]:
            lines.append(f"- {u['file']}: {u['uncovered']} uncovered (new coverage {u['coverage']}%)")
    return "\n".join(lines)


NEEDS_TOKEN = ("To read which condition failed, add a Sonar user token to .env as "
               "ASTA_SONAR_TOKEN (Sonar → My Account → Security → Generate Token).")
