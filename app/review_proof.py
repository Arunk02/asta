"""A review reads what it needs, and posts only what it can prove (12 Nov).

Six reviews posted as Arun, looked at side by side:
  * booking PR 1538 — two BLOCKING comments about plan shapes the flow never
    produces; the SIT booking the PR names has one leg. Reasoned from code shape.
  * booking PR 1535 / 1483 — one point raised; Copilot's review of the same diff
    found six concrete bugs it missed.
  * 1532 / 1536 — text lost or garbled on the way to GitHub.

So: a small change is read from its diff; a logic change also gets the callers of
what it changed, the workspace's own docs for that area, the real booking the PR
names, and the points other reviewers already raised. Then, before anything is
staged or posted, a stronger model tries to disprove each comment: proven stays,
unproven becomes a question, wrong is dropped. Nothing unproven is ever posted
as a finding.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

from . import untrusted

#: The verifier is a stronger model than the reviewer it checks, so it does not
#: share the reviewer's blind spots.
VERIFY_MODEL = "opus"


def verify_model(size: str) -> tuple[str, str]:
    """(model, effort) for the proof check. A logic change gets opus; a light one —
    tests, config, docs, build — is checked on the routing table's T2 cell: the
    points there are about shape, not about a flow opus has to trace (12 Nov,
    plan 2 E)."""
    if size == "logic":
        return VERIFY_MODEL, "high"
    from . import routing
    model, _ = routing._cell("claude", routing.T2)
    return model or "sonnet", "medium"

#: Beyond every proven blocking comment, at most this many others reach the PR.
#: 7 comments on PR 1481 were more than an author reads.
MAX_OTHER_COMMENTS = 3

#: Files whose change is light: tests, config, docs, build, CI.
_LIGHT = re.compile(
    r"(?:^|/)src/test/|(?:^|/)tests?/|\.(?:md|ya?ml|properties|json|feature|txt|xml|avsc)$|"
    r"(?:^|/)\.github/|(?:^|/)Dockerfile$|(?:^|/)pom\.xml$|(?:^|/)build\.gradle", re.I)

#: A method declared or changed in a Java/Kotlin diff line.
_METHOD = re.compile(
    r"(?:public|private|protected|static|final|synchronized|\s)+[\w<>\[\],.?\s]+?\s+(\w+)\s*\(")
_HUNK_CONTEXT = re.compile(r"^@@[^@]*@@\s*(.*)$", re.M)
_NOT_A_NAME = {"if", "for", "while", "switch", "catch", "return", "new", "equals", "hashCode",
               "toString", "get", "set", "of", "main", "builder", "build", "apply", "test"}


def size_of(meta: dict) -> str:
    """'light' when every changed file is a test, config, doc or build file; else 'logic'."""
    files = [f.get("path") or "" for f in (meta.get("files") or [])]
    if files and all(_LIGHT.search(p) for p in files):
        return "light"
    return "logic"


def changed_methods(diff: str, limit: int = 6) -> list[str]:
    """Methods the diff declares or changes, from hunk headers and added declarations."""
    names: list[str] = []
    sources = _HUNK_CONTEXT.findall(diff or "") + [
        line[1:] for line in (diff or "").splitlines()
        if line.startswith("+") and not line.startswith("+++")]
    for text in sources:
        m = _METHOD.search(text)
        if m and m.group(1) not in _NOT_A_NAME and m.group(1)[0].islower() \
                and m.group(1) not in names:
            names.append(m.group(1))
        if len(names) >= limit:
            break
    return names


async def callers(repo_dir: Path | None, names: list[str], per_name: int = 4,
                  total: int = 20) -> str:
    """Where the changed methods are called from, outside tests — the paths a change can break."""
    if not repo_dir or not (repo_dir / ".git").exists() or not names:
        return ""
    lines: list[str] = []
    for name in names:
        proc = await asyncio.create_subprocess_exec(
            "git", "grep", "-n", "-E", rf"(\.|::){name}\(|\b{name}\(", "--", "*.java", "*.kt",
            ":!**/src/test/**", cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
        except asyncio.TimeoutError:
            proc.kill()
            continue
        hits = [h for h in out.decode(errors="replace").splitlines()
                if not _METHOD.search(h.split(":", 2)[-1])][:per_name]   # calls, not the declaration
        lines += [f"{name}: {h[:200]}" for h in hits]
        if len(lines) >= total:
            break
    return "\n".join(lines[:total])


def domain_docs(meta: dict, workspace: str, names: list[str], budget: int = 3000) -> str:
    """The workspace's own docs for the area this PR changes."""
    from . import project_knowledge
    classes = {Path(f.get("path") or "").stem for f in (meta.get("files") or [])
               if not _LIGHT.search(f.get("path") or "")}
    question = " ".join([meta.get("title") or "", *sorted(classes)[:6], *names[:4]])
    try:
        return project_knowledge.lookup(question, workspace, budget=budget, channel="review")
    except Exception:                                            # noqa: BLE001
        return ""


def disputed_before(repo: str, limit: int = 5) -> str:
    """Comments on this repo an author showed were wrong — not to be raised again."""
    from . import review_outcomes
    rows = review_outcomes.disputes(repo)[-limit:]
    return "\n".join(f"- {r.get('path', '?')}: “{r.get('body', '')[:160]}” — author: "
                     f"“{r.get('reply', '')[:160]}”" for r in rows)


async def needs(meta: dict, workspace: str, repo_dir: Path | None) -> str:
    """What this review must read beyond the diff — sized to the change."""
    size = size_of(meta)
    if size == "light":
        return ("SIZE: light — tests, config, docs or build only. Read the diff; at most 3 "
                "points; no blockers unless something is plainly broken.\n")
    names = changed_methods(meta.get("diff") or "")
    parts = ["SIZE: logic — trace the callers below and check the change against the docs."]
    found = await callers(repo_dir, names)
    if found:
        parts.append("CALLERS of what changed (develop; open the ones that matter):\n"
                     + untrusted.wrap(found, "callers"))
    docs = domain_docs(meta, workspace, names)
    if docs:
        parts.append("DOMAIN — the workspace's own docs for this area:\n"
                     + untrusted.wrap(docs, "workspace docs"))
    repo = ((meta.get("headRepository") or {}).get("name") or "").lower()
    earlier = disputed_before(repo) if repo else ""
    if earlier:
        parts.append("DISPUTED BEFORE in this repo — authors showed these were wrong; do "
                     "not raise the same kind of point again:\n" + untrusted.wrap(earlier, "disputes"))
    return "\n\n".join(parts) + "\n"


# --- real data: the booking the PR itself names ---------------------------------------

def _payloads(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("data"), str):
            try:
                yield base64.b64decode(obj["data"]).decode("utf-8", "ignore")
            except (ValueError, TypeError):
                pass
        for v in obj.values():
            yield from _payloads(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _payloads(v)


def _first_list(obj, key: str):
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
        for x in obj.values():
            r = _first_list(x, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = _first_list(x, key)
            if r is not None:
                return r
    return None


def _first_value(obj, key: str):
    if isinstance(obj, dict):
        if isinstance(obj.get(key), (str, int)):
            return obj[key]
        for x in obj.values():
            r = _first_value(x, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = _first_value(x, key)
            if r is not None:
                return r
    return None


def plan_shape(payload: dict) -> str:
    """Order type, and each leg's start → end and the service-date triggers it carries."""
    legs = _first_list(payload, "servicePlanLegs") or []
    if not legs:
        return ""

    def fn(loc) -> str:
        return (loc.get("locationFunction") if isinstance(loc, dict) else loc) or "?"

    def trig(d) -> str:
        t = d.get("eventTrigger") if isinstance(d, dict) else ""
        return (t.get("eventTriggerEnumValue") if isinstance(t, dict) else t) or "?"
    rows = [f"order type {_first_value(payload, 'orderType') or '?'} · {len(legs)} leg(s)"]
    for i, leg in enumerate(legs[:8]):
        dates = [trig(d) for d in (leg.get("serviceDates") or [])][:8]
        rows.append(f"  leg {i}: {fn(leg.get('startLocation'))} → {fn(leg.get('endLocation'))}"
                    f" · dates: {', '.join(dates) or 'none'}")
    return "\n".join(rows)


async def real_input(meta: dict) -> str:
    """The shape of the booking the PR names, read-only from Temporal — '' when it names none."""
    from . import booking_case, temporal
    text = f"{meta.get('title') or ''}\n{meta.get('body') or ''}"
    env = booking_case.env_of(text)
    if not env or not temporal.enabled():
        return ""
    for booking in booking_case.ids(text)[:3]:
        try:
            rows = await temporal.workflows(env, f'WorkflowId STARTS_WITH "{booking}"', limit=5)
        except Exception:                                        # noqa: BLE001
            continue
        for row in rows:
            wid = ((row.get("execution") or {}).get("workflowId") or row.get("workflowId") or "")
            if not wid:
                continue
            try:
                events = await temporal.history(env, wid)
            except Exception:                                    # noqa: BLE001
                continue
            for raw in _payloads(events[:60]):
                if '"servicePlanLegs"' not in raw:
                    continue
                try:
                    shape = plan_shape(json.loads(raw))
                except ValueError:
                    continue
                if shape:
                    return f"{booking} in {env} ({wid}):\n{shape}"
    return ""


# --- the check before anything is staged or posted --------------------------------------

async def _code_at_head(meta: dict, findings: list[dict], around: int = 60) -> str:
    """The lines around each comment, as the PR head has them."""
    from . import review
    target = (meta.get("url") or "").split("github.com/")[-1].split("/pull/")[0]
    sha = meta.get("headRefOid") or ""
    if not target or not sha:
        return ""
    out, seen = [], set()
    for f in findings[:8]:
        if f["path"] in seen:
            continue
        seen.add(f["path"])
        rc, body = await review._gh(Path.home(), "api", "-H", "Accept: application/vnd.github.raw",
                                    f"repos/{target}/contents/{f['path']}?ref={sha}", timeout=30)
        if rc != 0:
            continue
        lines = body.splitlines()
        marks = sorted({g["line"] for g in findings if g["path"] == f["path"]})
        lo, hi = max(0, marks[0] - around), min(len(lines), marks[-1] + around)
        window = "\n".join(f"{i + 1}: {lines[i]}" for i in range(lo, hi))
        out.append(f"### {f['path']} (lines {lo + 1}-{hi})\n{window[:12000]}")
    return "\n\n".join(out)


def _numbered(findings: list[dict]) -> str:
    return "\n".join(
        f"{i}. `{f['path']}:{f['line']}` [{'BLOCKING' if f.get('blocking') else 'non-blocking'}] "
        f"{f['body']}" + (f" (reviewer's proof: {f['proof']})" if f.get("proof") else "")
        for i, f in enumerate(findings, 1))


VERIFY_BRIEF = """You check review comments before they are posted under Arun's name on a
colleague's pull request. A comment that turns out wrong costs his credibility, so each
must be PROVEN or it is not posted as a finding.

For each numbered comment, decide:
- proven — you can name what makes it happen: a caller (path:line) that reaches it, an
  existing test, the workspace docs below, or the real data below; AND the code at the PR
  head really behaves as the comment says.
- unproven — plausible, but nothing shown makes it happen (a shape the flow may never
  produce, an input nobody sends). "Nothing in the code prevents X" is NOT proof that X
  happens.
- wrong — the code at the PR head, the docs or the real data contradict it: also when the
  real data or docs show the input it depends on has another shape (the booking has one
  leg; the comment needs several) and nothing shows the shape it needs ever occurs; and
  when it is not a defect at all — a "confirm/consider" note, or code that matches how the
  codebase already does the same thing elsewhere.
Open the files you need in this repository. Do not change anything.

Reply with ONLY a JSON array, one object per comment, nothing else:
[{{"n": 1, "verdict": "proven|unproven|wrong", "proof": "≤ 20 words: what settles it"}}]

{pr}

COMMENTS:
{comments}

CODE AT THE PR HEAD:
{code}

{docs}{data}DIFF:
{diff}
"""


def _verdicts(said: str, count: int) -> dict[int, dict]:
    m = re.search(r"\[\s*\{.*\}\s*\]", said or "", re.S)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(0))
    except ValueError:
        return {}
    out = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            n = int(r.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        v = str(r.get("verdict") or "").lower().strip()
        if 1 <= n <= count and v in ("proven", "unproven", "wrong"):
            out[n] = {"verdict": v, "proof": str(r.get("proof") or "")[:200]}
    return out


def apply(findings: list[dict], verdicts: dict[int, dict], action: str) -> tuple[list[dict], str, list[str]]:
    """Proven stays; unproven becomes a question; wrong is dropped; then the cap.

    A finding with no verdict (the check failed or skipped it) counts as unproven:
    nothing unproven is posted as a finding."""
    kept, report = [], []
    for i, f in enumerate(findings, 1):
        v = verdicts.get(i) or {"verdict": "unproven", "proof": "not checked"}
        where = f"{f['path'].rsplit('/', 1)[-1]}:{f['line']}"
        if f["body"].startswith("Question:"):
            kept.append(dict(f, blocking=False))
            report.append(f"? {where} — a question as written")
        elif v["verdict"] == "proven":
            kept.append(dict(f, proof=v["proof"]))
            report.append(f"✓ {where} — proven: {v['proof']}")
        elif v["verdict"] == "unproven":
            kept.append(dict(f, blocking=False, body="Question: " + f["body"], proof=""))
            report.append(f"? {where} — not proven, asked as a question ({v['proof']})")
        else:
            report.append(f"✗ {where} — dropped: {v['proof']}")
    blocking = [f for f in kept if f.get("blocking")]
    others = [f for f in kept if not f.get("blocking")]
    if len(others) > MAX_OTHER_COMMENTS:
        report.append(f"… {len(others) - MAX_OTHER_COMMENTS} lesser point(s) left out")
        others = others[:MAX_OTHER_COMMENTS]
    if action == "request_changes" and not blocking:
        action = "comment"          # nothing proven blocks the merge
    return blocking + others, action, report


async def check(findings: list[dict], action: str, meta: dict, workspace: str,
                repo_dir: Path | None) -> tuple[list[dict], str, list[str]]:
    """Every comment checked for proof before it is staged or posted."""
    from . import claude_cli, review
    if not findings:
        return findings, action, []
    code = await _code_at_head(meta, findings)
    size = size_of(meta)
    logic = size == "logic"
    model, effort = verify_model(size)
    docs = domain_docs(meta, workspace, changed_methods(meta.get("diff") or "")) if logic else ""
    data = await real_input(meta) if logic else ""
    header = (f"PR #{meta.get('number')}: {meta.get('title')}\n"
              f"Description:\n{(meta.get('body') or '(none)')[:2000]}")
    prompt = VERIFY_BRIEF.format(
        pr=untrusted.wrap(header, "pull request"),
        comments=_numbered(findings),
        code=untrusted.wrap(code or "(could not read the files at the PR head)", "code at head"),
        docs=("WORKSPACE DOCS:\n" + untrusted.wrap(docs, "workspace docs") + "\n\n") if docs else "",
        data=("REAL DATA (read-only, from Temporal):\n" + untrusted.wrap(data, "real data") + "\n\n")
        if data else "",
        diff=untrusted.wrap(review._prioritise_diff(meta.get("diff") or "")[:15000], "diff"))
    try:
        said = await claude_cli.one_shot(
            prompt, cwd=str(repo_dir) if repo_dir and repo_dir.exists() else None, timeout=480,
            effort=effort, plan_only=True, model=model, purpose="review-proof")
    except Exception:                                             # noqa: BLE001
        said = ""
    verdicts = _verdicts(said, len(findings))
    kept, action, report = apply(findings, verdicts, action)
    if not verdicts:
        report.insert(0, "⚠️ the proof check could not run — every point is asked as a question")
    elif data:
        report.insert(0, f"real data read: {data.splitlines()[0]}")
    return kept, action, report


async def check_pr(findings: list[dict], action: str, pr: str, workspace: str,
                   repo: str = "") -> tuple[list[dict], str, list[str]]:
    """`check`, reading the PR first. Raises when the PR cannot be read."""
    from . import review
    if not findings:
        return findings, action, []
    meta = await review.gather(pr, workspace, repo)
    return await check(findings, action, meta, workspace, review.repo_checkout(meta, workspace, repo))


def tally(report: list[str]) -> str:
    """'Proof check: 2 proven, 2 asked as questions, 1 dropped. ' — or '' when none ran."""
    proven = sum(1 for r in report if r.startswith("✓"))
    asked = sum(1 for r in report if r.startswith("?"))
    dropped = sum(1 for r in report if r.startswith("✗"))
    if not (proven or asked or dropped):
        return ""
    parts = [f"{proven} proven"] + ([f"{asked} asked as questions"] if asked else []) \
        + ([f"{dropped} dropped"] if dropped else [])
    return "Proof check: " + ", ".join(parts) + ". "
