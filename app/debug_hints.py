"""Facts a debugging turn should start from, read off the words — not guessed.

13 Nov: a colleague's ask named an AP activity class and gave a Temporal link
for a SIT workflow. The chat brain read billing logs only, then moved to QA on
its own and answered from there; the class's own service and the workflow were
never read. Three things settle where to look before any query runs:

* the environment he named (it stays put until he names another),
* the repo that owns a named class,
* the workflow a Temporal link points at.
"""
from __future__ import annotations

import re
import subprocess
import time
import urllib.parse
from pathlib import Path

ENV_SECONDS = 3600
_ENV = re.compile(r"\b(prod|preprod|pre-prod|uat|sit|qa|dev)\b", re.I)
#: A turn about something failing — only then is an environment worth pinning.
_DEBUGGY = re.compile(r"\b(fail\w*|error\w*|exception\w*|logs?|stuck|why|issue\w*|temporal|workflow\w*|crash\w*|timeout\w*|violation\w*|check)\b", re.I)
_CLASS = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)*(?:Impl|Service|Activity|Workflow|"
                    r"Controller|Factory|Handler|Listener|Consumer|Producer|Mapper|Resolver|"
                    r"Processor|Client|Repository)\b")
_TEMPORAL = re.compile(r"/namespaces/([A-Za-z0-9._-]+)/workflows(?:/([^/?\s]+))?([^\s]*)")
_STARTS = re.compile(r'STARTS_WITH\s*"([^"]+)"')


def _env_of(text: str) -> str:
    m = _ENV.search(text or "")
    if not m:
        return ""
    e = m.group(1).lower().replace("pre-prod", "preprod")
    return e


def pinned_env(cid: str, his_text: str, now: float | None = None) -> tuple[str, str]:
    """(env, when) he named last in this conversation within the hour, or ("", "")."""
    env = _env_of(his_text)
    if env:
        return env, "now"
    from . import store
    now = time.time() if now is None else now
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT content, created_at FROM ui_messages WHERE conv_id=? AND role='user' "
            "AND created_at>=? ORDER BY id DESC LIMIT 20", (cid, now - ENV_SECONDS)).fetchall()
    for content, at in rows:
        # His words only — the wrapper Asta puts around feedback is not his.
        said = (content or "").split("His feedback:", 1)[-1]
        env = _env_of(said)
        if env:
            return env, time.strftime("%H:%M", time.localtime(float(at)))
    return "", ""


def _repo_roots() -> list[Path]:
    from . import workspace_tools, worktrees
    roots: list[Path] = []
    for root in workspace_tools.WORKSPACES.values():
        try:
            roots += worktrees.repos_in(Path(root))
        except Exception:                                      # noqa: BLE001
            continue
    return roots


def owner_of(cls: str, roots: list[Path] | None = None) -> str:
    """The repo whose code declares this class, or ""."""
    for repo in (roots if roots is not None else _repo_roots()):
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "grep", "-l", "-E",
                 rf"(class|interface|enum|record)\s+{re.escape(cls)}\b"],
                capture_output=True, text=True, timeout=5)
        except Exception:                                      # noqa: BLE001
            continue
        if out.returncode == 0 and out.stdout.strip():
            return repo.name
    return ""


def temporal_refs(text: str) -> list[tuple[str, str]]:
    """(namespace, workflow id or id prefix) for each Temporal web link."""
    found = []
    for m in _TEMPORAL.finditer(text or ""):
        ns, wid, rest = m.group(1), urllib.parse.unquote(m.group(2) or ""), m.group(3) or ""
        if not wid:
            q = _STARTS.search(urllib.parse.unquote_plus(rest).replace("`", ""))
            wid = q.group(1) if q else ""
        if (ns, wid) not in found:
            found.append((ns, wid))
    return found


def hints(cid: str, his_text: str, full_text: str, roots: list[Path] | None = None,
          now: float | None = None) -> str:
    """One bracketed block for the turn, or "" when there is nothing to pin."""
    lines = []
    refs = temporal_refs(full_text)
    # His words first, then the rest; up to ten looked up, three reported. The
    # first three in a long case were often not his repos' classes at all.
    classes = list(dict.fromkeys(_CLASS.findall(his_text or "") + _CLASS.findall(full_text or "")))[:10]
    if not (refs or classes or _DEBUGGY.search(his_text or "")):
        return ""
    env, when = pinned_env(cid, his_text, now) if cid else (_env_of(his_text), "now")
    if not env:
        env = next((_env_of(ns) for ns, _ in refs if _env_of(ns)), "")
        when = "the Temporal link" if env else ""
    if env:
        said = "he named it this message" if when == "now" else f"named at {when}"
        lines.append(f"Environment: {env.upper()} ({said}). Every log and Temporal read "
                     f"stays in {env.upper()}; another environment only if he names it.")
    for ns, wid in refs:
        lines.append(f"Temporal link: namespace {ns}, workflow {wid or '(open the link)'}"
                     f" — read its history with temporal_workflow before the logs.")
    named = 0
    for cls in classes:
        if named >= 3:
            break
        repo = owner_of(cls, roots)
        if repo:
            named += 1
            lines.append(f"{cls} is declared in {repo} — start from that service's logs "
                         f"and code; downstream services come after.")
    if not lines:
        return ""
    return "[Where to look — read from the words, not guessed:\n- " + "\n- ".join(lines) + "]"
