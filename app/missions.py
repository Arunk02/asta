"""Missions: Jira ticket / request → drafted plan → approval → headless implementation → Claude test pass.

Executors (ASTA_EXECUTOR or per-mission):
  copilot - GitHub Copilot CLI  (copilot -p ... --allow-all-tools)  ← "trigger IntelliJ's brain" headlessly
  claude  - Claude Code CLI     (claude -p ... --permission-mode acceptEdits)
  echo    - dry-run simulator for pipeline testing

The verify phase always runs Claude to test/review the diff, per Arun's flow.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from pydantic_ai import Agent

from . import agent as agent_mod
from . import jira, notify, store, workspace_tools

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "data" / "missions"

EXEC_TIMEOUT = 45 * 60

PLAN_PROMPT = """You are drafting an implementation plan for a developer mission.

{jira_block}
Mission: {title}
{description_block}
Workspace: {workspace} (services: {services})
Target repo: {repo}

Contmark context resolution (exact files/flows relevant to this task):
{context}

Write a concrete, step-by-step implementation plan:
- affected service(s) and files (from the context above),
- the changes to make in each,
- tests to add/update and how to run them,
- risks/rollback notes.
Keep it under 40 lines. No preamble."""

IMPLEMENT_PROMPT = """Implement the following approved plan in this repository. Make the code changes,
add/update tests as described, and run the test suite. Fix failures you introduced.

PLAN:
{plan}

Do NOT commit, push, or open a pull request — Arun reviews the diff first and a separate
step ships it. If you do commit for any reason, use a plain message with no co-author
trailer and no mention of any AI assistant.

When done, print a short summary of files changed and test results."""

VERIFY_PROMPT = """You are the independent test/review pass. Review the uncommitted changes in this
repository (git status / git diff), check them against this plan, and run the relevant tests.

PLAN:
{plan}

Print a verdict line first: VERDICT: PASS or VERDICT: FAIL, then a short justification
and any test output summary. Do not make code changes unless a test is trivially broken."""


def playbook_block(repo_dir: Path) -> str:
    """Point the executor at the repo's own project context agent playbooks/skills, if present.

    Arun's workspaces ship .github/agents (implement, unit-test, component-test, review) and .github/skills (build profiles,
    Spring/Kotlin conventions, Kafka/Temporal patterns). Headless executors must code
    and test the way those playbooks specify, not their own defaults.
    """
    for base in (repo_dir / ".github", repo_dir.parent / ".github"):
        if (base / "agents").is_dir() or (base / "skills").is_dir():
            return (
                f"\n\nIMPORTANT — this codebase ships its own engineering playbooks under {base}:\n"
                "- agents/implement.agent.md — how implementation is done here "
                "(boot skills, build command from pins, prohibited actions)\n"
                "- agents/unit-test.agent.md and component-test.agent.md — "
                "how tests must be written\n"
                "- skills/ — build profiles (maven/gradle), language conventions "
                "(spring-java/kotlin/react), domain patterns (kafka/temporal)\n"
                "BEFORE coding: read the implement agent + the convention and build skills "
                "relevant to this stack, and follow them exactly. Write unit/component tests "
                "per the test agents' conventions."
            )
    return ""


def executor_cmd(executor: str, prompt: str) -> list[str]:
    if executor == "claude":
        return ["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
    if executor == "copilot":
        return ["copilot", "-p", prompt, "--allow-all-tools", "--allow-all-paths"]
    if executor == "echo":
        return ["/bin/sh", "-c", "echo '[echo executor] would run:'; echo " + _sh_quote(prompt[:400]) + "; echo 'VERDICT: PASS'"]
    raise ValueError(f"Unknown executor '{executor}' (use copilot | claude | echo)")


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def default_executor() -> str:
    return os.environ.get("ASTA_EXECUTOR", "copilot")


def _repo_dir(mission: dict) -> Path:
    ws_root = Path(workspace_tools.WORKSPACES[mission["workspace"]])
    return ws_root / mission["repo"] if mission["repo"] else ws_root


# --- lifecycle ---------------------------------------------------------------

async def start(title: str, workspace: str, repo: str | None = None, jira_key: str | None = None,
                description: str = "", executor: str | None = None) -> dict:
    if workspace not in workspace_tools.WORKSPACES:
        raise ValueError(f"workspace must be one of: {', '.join(workspace_tools.WORKSPACES)}")
    mission = store.create_mission(
        title=title, workspace=workspace, repo=repo, jira_key=jira_key,
        description=description, executor=executor or default_executor(),
    )
    asyncio.create_task(_plan(mission["id"]))
    return mission


async def _plan(mission_id: int) -> None:
    m = store.get_mission(mission_id)
    try:
        jira_block = ""
        if m["jira_key"] and jira.configured():
            issue = await jira.get_issue(m["jira_key"])
            jira_block = (
                f"Jira {issue['key']} [{issue['type']} / {issue['status']} / {issue['priority']}]: "
                f"{issue['summary']}\n{issue['description']}\n"
            )
        context = await workspace_tools.resolve_context(m["workspace"], m["title"] + " " + m["description"])
        services = ", ".join(workspace_tools.list_services(m["workspace"]))
        prompt = PLAN_PROMPT.format(
            jira_block=jira_block,
            title=m["title"],
            description_block=("Details: " + m["description"]) if m["description"] else "",
            workspace=m["workspace"],
            services=services,
            repo=m["repo"] or "(decide from context)",
            context=context[:12000],
        )
        try:
            model = agent_mod.get_model(agent_mod.best_model_name())
            result = await Agent(model=model).run(prompt)
            plan = result.output
        except RuntimeError:
            # No API model available — plan with the office Copilot CLI instead.
            from . import copilot_cli
            plan = await copilot_cli.one_shot(prompt, cwd=str(_repo_dir(m)))
        store.update_mission(mission_id, plan=plan, status="awaiting_approval")
        await notify.notify(
            f"📋 Mission #{mission_id} plan ready: {m['title']}\n"
            f"Reply 'approve mission {mission_id}' or open the Missions tab.", "action"
        )
    except Exception as exc:
        store.update_mission(mission_id, status="failed", error=f"planning: {exc}")
        await notify.notify(f"❌ Mission #{mission_id} planning failed: {exc}", "error")


async def approve(mission_id: int) -> dict:
    m = store.get_mission(mission_id)
    if not m:
        raise ValueError(f"No mission #{mission_id}")
    if m["status"] != "awaiting_approval":
        raise ValueError(f"Mission #{mission_id} is '{m['status']}', not awaiting approval")
    store.update_mission(mission_id, status="implementing")
    asyncio.create_task(_execute(mission_id))
    return store.get_mission(mission_id)


async def reject(mission_id: int) -> dict:
    m = store.get_mission(mission_id)
    if not m:
        raise ValueError(f"No mission #{mission_id}")
    store.update_mission(mission_id, status="rejected")
    return store.get_mission(mission_id)


async def _run_phase(cmd: list[str], cwd: Path, log, phase: str) -> int:
    log.write(f"\n===== {phase} @ {time.strftime('%H:%M:%S')} : {' '.join(cmd[:2])} =====\n".encode())
    log.flush()
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), stdout=log, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "CI": "1"},
    )
    try:
        return await asyncio.wait_for(proc.wait(), timeout=EXEC_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        log.write(f"\n[timeout after {EXEC_TIMEOUT}s]\n".encode())
        return -1


async def _execute(mission_id: int) -> None:
    m = store.get_mission(mission_id)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{mission_id}.log"
    store.update_mission(mission_id, log_path=str(log_path))
    cwd = _repo_dir(m)
    try:
        with open(log_path, "ab") as log:
            rc = await _run_phase(
                executor_cmd(m["executor"],
                             IMPLEMENT_PROMPT.format(plan=m["plan"]) + playbook_block(cwd)),
                cwd, log, f"IMPLEMENT ({m['executor']})",
            )
            if rc != 0:
                raise RuntimeError(f"implementation exited with code {rc} — see log")
            store.update_mission(mission_id, status="testing")
            verify_exec = "echo" if m["executor"] == "echo" else "claude"
            rc = await _run_phase(
                executor_cmd(verify_exec, VERIFY_PROMPT.format(plan=m["plan"])),
                cwd, log, "VERIFY (claude)",
            )
        tail = log_path.read_text(errors="replace")[-2000:]
        passed = rc == 0 and "VERDICT: PASS" in tail
        store.update_mission(mission_id, status="done" if passed else "failed",
                             error="" if passed else "verify verdict not PASS — see log")
        icon = "✅" if passed else "❌"
        nxt = ("\nSay \"raise the PR for #%d\" and I'll commit, push, open the PR and "
               "watch CI." % mission_id) if passed else ""
        await notify.notify(
            f"{icon} Mission #{mission_id} {'complete — code done' if passed else 'needs attention'}: {m['title']}\n"
            f"Repo: {cwd}\nReview the diff in IntelliJ. Log: {log_path.name}{nxt}",
            "action", urgency="direct")
    except Exception as exc:
        store.update_mission(mission_id, status="failed", error=str(exc)[:500])
        await notify.notify(f"❌ Mission #{mission_id} failed: {exc}", "error")


# --- ship: commit → push → PR → CI ------------------------------------------

# Arun's rule: commits and PRs must look like HIS work. No Claude/Copilot
# co-author trailers, no "Generated with" footers — which tool he used is his
# business, and it would show up in the PR for the whole team to see.
NO_ATTRIBUTION = (
    "\n\nCOMMIT RULES (strict):\n"
    "- Use plain `git commit -m \"<message>\"`. NOTHING else in the message.\n"
    "- NEVER add a Co-Authored-By trailer, an AI/assistant name, an emoji robot, "
    "or any 'Generated with …' line. The commit must read as Arun's own work.\n"
    "- Do not pass --author or amend authorship; the repo's configured identity is correct.\n"
)


async def _git(cwd: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"timed out: {' '.join(args)}"
    return proc.returncode or 0, raw.decode(errors="replace")


def _branch_name(m: dict) -> str:
    if m.get("jira_key"):
        return f"feature/{m['jira_key']}"
    slug = re.sub(r"[^a-z0-9]+", "-", (m["title"] or "change").lower()).strip("-")[:40]
    return f"feature/asta-{m['id']}-{slug}"


async def ship(mission_id: int, review_chat: str = "") -> dict:
    """Commit, push, open a PR, then watch CI — notifying Arun at each step.

    Deliberately NOT automatic: a mission finishing is not permission to publish.
    Arun triggers this ("raise the PR") once he's happy with the diff.
    """
    m = store.get_mission(mission_id)
    if not m:
        raise RuntimeError(f"no mission #{mission_id}")
    cwd = _repo_dir(m)
    branch = _branch_name(m)
    subject = (f"{m['jira_key']}: " if m.get("jira_key") else "") + (m["title"] or "changes")

    rc, out = await _git(cwd, "git", "status", "--porcelain")
    if rc != 0:
        raise RuntimeError(f"git status failed: {out[:200]}")
    if not out.strip():
        raise RuntimeError("nothing to commit — the working tree is clean")

    cur = (await _git(cwd, "git", "rev-parse", "--abbrev-ref", "HEAD"))[1].strip()
    if cur in ("main", "master", "develop"):
        rc, out = await _git(cwd, "git", "checkout", "-b", branch)
        if rc != 0:
            raise RuntimeError(f"could not create branch {branch}: {out[:200]}")
    else:
        branch = cur  # already on a feature branch — respect it

    rc, out = await _git(cwd, "git", "add", "-A")
    if rc != 0:
        raise RuntimeError(f"git add failed: {out[:200]}")
    # Plain commit: no trailers, no attribution (see NO_ATTRIBUTION).
    rc, out = await _git(cwd, "git", "commit", "-m", subject)
    if rc != 0:
        raise RuntimeError(f"git commit failed: {out[:300]}")
    await notify.notify(f"✅ Code committed for mission #{mission_id} ({branch}): {subject}",
                        "action", urgency="direct")

    rc, out = await _git(cwd, "git", "push", "-u", "origin", branch, timeout=300)
    if rc != 0:
        raise RuntimeError(f"git push failed: {out[:300]}")

    rc, out = await _git(cwd, "gh", "pr", "create", "--fill", "--head", branch, timeout=300)
    if rc != 0 and "already exists" not in out:
        raise RuntimeError(f"gh pr create failed: {out[:300]}")
    pr_url = ""
    mu = re.search(r"https://github\.com/\S+/pull/\d+", out)
    if mu:
        pr_url = mu.group(0)
    else:
        rc2, out2 = await _git(cwd, "gh", "pr", "view", "--json", "url", "--jq", ".url")
        pr_url = out2.strip() if rc2 == 0 else "(PR url unavailable)"

    store.update_mission(mission_id, error="")  # clear any stale error text
    await notify.notify(f"🔀 PR raised for mission #{mission_id}: {pr_url}",
                        "action", urgency="direct")
    asyncio.create_task(_watch_ci(mission_id, cwd, branch, pr_url, review_chat))
    return {"branch": branch, "pr": pr_url, "committed": subject}


CI_WATCH_MAX_SECONDS = 60 * 60


async def _watch_ci(mission_id: int, cwd: Path, branch: str, pr_url: str,
                    review_chat: str = "") -> None:
    """Poll this branch's checks until they settle, then tell Arun the outcome.

    On success it ASKS whether to post the PR for review — it never posts to a
    person or group on its own.
    """
    deadline = asyncio.get_event_loop().time() + CI_WATCH_MAX_SECONDS
    await asyncio.sleep(45)  # give the workflow time to register
    while asyncio.get_event_loop().time() < deadline:
        rc, out = await _git(cwd, "gh", "pr", "checks", "--json",
                             "name,state,link", timeout=90)
        if rc == 0 and out.strip():
            try:
                checks = json.loads(out)
            except Exception:
                checks = []
            states = [(c.get("state") or "").upper() for c in checks]
            if states and not any(s in ("PENDING", "QUEUED", "IN_PROGRESS", "") for s in states):
                bad = [c for c, s in zip(checks, states)
                       if s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT")]
                if bad:
                    await notify.notify(
                        f"🔴 CI FAILED on your PR (mission #{mission_id}):\n"
                        + "\n".join(f"• {c['name']}" for c in bad[:5])
                        + f"\n{pr_url}\nWant me to look at the logs and fix it?",
                        "action", urgency="direct")
                else:
                    ask = (f"Want me to post it in {review_chat} for review?" if review_chat
                           else "Want me to post it anywhere for review? Tell me the person or "
                                "group and I'll send it.")
                    await notify.notify(
                        f"🟢 CI PASSED on your PR (mission #{mission_id}): {pr_url}\n{ask}",
                        "action", urgency="direct")
                return
        await asyncio.sleep(60)
    await notify.notify(
        f"⏳ CI on mission #{mission_id} hasn't finished after an hour: {pr_url}",
        "action", urgency="direct")


def log_tail(mission_id: int, chars: int = 4000) -> str:
    m = store.get_mission(mission_id)
    if not m or not m["log_path"] or not Path(m["log_path"]).exists():
        return ""
    return Path(m["log_path"]).read_text(errors="replace")[-chars:]
