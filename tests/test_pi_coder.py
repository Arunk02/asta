"""ASTA_CODER=pi: Claude plans, pi builds, Claude takes over whenever pi can't (15 Nov)."""

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest

from app import context_pack, pi_cli, routing, store, tasks, worker_usage
from app.graph import code_graph


@pytest.fixture
def task():
    t = store.create_task("Map the equipment size", "code", "map it", workspace=None)
    store.kv_set(f"task_executor:{t['id']}", "claude")
    return t["id"]


@pytest.fixture
def pi_on(monkeypatch):
    monkeypatch.setenv("ASTA_CODER", "pi")
    monkeypatch.setattr(pi_cli, "available", lambda: True)


# --- who runs which leg -------------------------------------------------------

def test_claude_unless_he_flips_the_one_flag(monkeypatch, task):
    monkeypatch.delenv("ASTA_CODER", raising=False)
    tasks.mark_approved(task)
    assert tasks.coder() == "claude"
    assert tasks.leg_executor(task) == "claude"


def test_claude_plans_and_pi_builds(pi_on, task):
    assert tasks.leg_executor(task) == "claude", "before his yes it is a planning leg"
    tasks.mark_approved(task)
    assert tasks.leg_executor(task) == "pi"


def test_pi_out_means_claude_builds(pi_on, task):
    tasks.mark_approved(task)
    pi_cli.mark_down("Third-party apps now draw from your extra usage")
    assert tasks.leg_executor(task) == "claude"
    pi_cli.mark_ok()
    assert tasks.leg_executor(task) == "pi"


def test_stuck_twice_on_pi_escalates_to_claude_once(pi_on, monkeypatch, task):
    from app import agent, claude_cli
    tasks.mark_approved(task)
    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(agent, "quota_down", lambda b: False)
    assert tasks._stronger_executor(task) == "claude"
    store.kv_set(f"task_verify_escbrain:{task}", "1")
    assert tasks.leg_executor(task) == "claude"


def test_the_first_build_leg_starts_fresh_with_the_plan_in_full(pi_on, task):
    store.kv_set(f"task_session:{task}:claude", "plan-session")
    tasks.mark_approved(task)
    assert not code_graph._has_session(task), "pi holds no session yet"
    plan = "STRUCTURE\nEquipmentMapper.java\n" + "step detail. " * 600     # ~7.8k chars
    store.kv_set(f"task_plan:{task}", plan)
    pack = context_pack.build(task, "implement", handover=True)
    assert "Another brain planned this task" in pack
    assert plan.strip()[-200:] in pack, "the whole plan, not an excerpt"
    assert plan.strip()[-200:] not in context_pack.build(task, "implement")


def test_a_reset_clears_the_pi_session_too(task):
    assert "pi" in tasks._session_brains()


def test_size_check_reads_a_pi_session(pi_on, monkeypatch, task):
    tasks.mark_approved(task)
    store.kv_set(f"task_session:{task}:pi", "pi-sid")
    monkeypatch.setattr(pi_cli, "context_tokens", lambda sid: 200_000)
    assert code_graph._too_large(task, store.get_task(task)) == 200_000


# --- falling back -------------------------------------------------------------

def test_a_pi_account_error_builds_on_claude_and_says_so_once(pi_on, monkeypatch, task):
    from app import notify
    tasks.mark_approved(task)
    told, ran = [], []

    async def _say(text, *a, **k):
        told.append(text)

    async def _cli(task_id, executor, prompt, **kw):
        ran.append(executor)
        if executor == "pi":
            raise pi_cli.Unavailable("Third-party apps now draw from your extra usage")
        return "built on claude"

    monkeypatch.setattr(notify, "notify", _say)
    monkeypatch.setattr(tasks, "_observed_cli", _cli)
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "context_boot_paths", lambda p, *a: p)
    out = asyncio.run(tasks._code_leg(task, "build it", "/tmp", resume=False, effort="high"))
    assert out == "built on claude" and ran == ["pi", "claude"]
    assert len(told) == 1 and "pi can't build" in told[0]
    store.kv_set(f"task_session:{task}:pi", "")
    asyncio.run(tasks._code_leg(task, "next leg", "/tmp", resume=False, effort="high"))
    assert ran[-1] == "claude" and len(told) == 1, "pi stays out; he is not told twice"


# --- the pi runner, against a stand-in pi ---------------------------------------

FAKE_PI = textwrap.dedent('''\
    #!{py}
    import json, sys
    args = sys.argv[1:]
    open({log!r}, "w").write(json.dumps(args))
    mode = {mode!r}
    def say(o): print(json.dumps(o), flush=True)
    first = json.loads(sys.stdin.readline())
    say({{"type": "response", "command": "prompt", "success": True}})
    if mode == "account":
        say({{"type": "message_end", "message": {{"role": "assistant", "content": [],
             "stopReason": "error", "errorMessage": "400 Third-party apps now draw from your extra usage"}}}})
        say({{"type": "agent_settled"}}); sys.exit(0)
    steered = ""
    if mode == "steer":
        say({{"type": "tool_execution_start", "toolName": "bash"}})
        steered = json.loads(sys.stdin.readline())["message"]
    say({{"type": "message_end", "message": {{"role": "assistant", "content": [
        {{"type": "text", "text": "DONE " + first["message"][-12:] + (" | " + steered if steered else "")}}],
        "usage": {{"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0}}}}}})
    say({{"type": "agent_settled"}})
''')


def _fake_pi(tmp_path, monkeypatch, mode="ok"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "pi"
    log = tmp_path / "args.json"
    exe.write_text(FAKE_PI.format(py=sys.executable, log=str(log), mode=mode))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(pi_cli, "SESSIONS", tmp_path / "sessions")
    return log


def test_one_leg_runs_over_rpc_with_the_fence_and_returns_the_answer(tmp_path, monkeypatch):
    log = _fake_pi(tmp_path, monkeypatch)
    out = asyncio.run(pi_cli.one_shot("implement the plan", cwd=str(tmp_path),
                                      session_id="s-1", effort="high", model="opus"))
    assert out.startswith("DONE ") and "nt the plan" in out
    args = json.loads(log.read_text())
    assert args[:2] == ["--mode", "rpc"]
    assert args[args.index("--session-id") + 1] == "s-1"
    assert args[args.index("--model") + 1] == "anthropic/claude-opus-5"
    assert args[args.index("--thinking") + 1] == "high"
    assert "--no-extensions" in args and args[args.index("-e") + 1].endswith("pi_fence.ts")


def test_an_account_error_is_unavailable_not_a_crash(tmp_path, monkeypatch):
    _fake_pi(tmp_path, monkeypatch, mode="account")
    with pytest.raises(pi_cli.Unavailable):
        asyncio.run(pi_cli.one_shot("x", cwd=str(tmp_path), session_id="s-2"))


def test_his_words_reach_a_build_while_it_runs(tmp_path, monkeypatch, task):
    _fake_pi(tmp_path, monkeypatch, mode="steer")

    async def run():
        leg = asyncio.create_task(pi_cli.one_shot("build", cwd=str(tmp_path),
                                                  session_id="s-3", task_id=task))
        for _ in range(100):
            if pi_cli.building(task):
                break
            await asyncio.sleep(0.02)
        reply = tasks.augment(task, "also keep the old field name", code_change=True)
        return reply, await leg

    reply, out = asyncio.run(run())
    assert "while it builds" in reply
    assert "also keep the old field name" in out and "Arun said this himself" in out
    assert not store.kv_get(f"task_addenda:{task}"), "steered, so not held for a gate too"


def test_without_a_live_build_his_words_wait_for_the_gate(task):
    tasks.augment(task, "also keep the old field name", code_change=True)
    assert "old field name" in store.kv_get(f"task_addenda:{task}")


def test_a_pi_leg_is_measured_from_its_own_session(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_cli, "SESSIONS", tmp_path)
    f = tmp_path / "2026-11-15T10-00-00-000Z_sid-9.jsonl"
    rows = [{"type": "session"},
            {"type": "message", "message": {"role": "assistant", "usage":
             {"input": 100, "output": 20, "cacheRead": 4000, "cacheWrite": 900}}},
            {"type": "message", "message": {"role": "assistant", "usage":
             {"input": 50, "output": 10, "cacheRead": 5000, "cacheWrite": 0}}}]
    f.write_text("\n".join(json.dumps(r) for r in rows))
    assert worker_usage._snapshot("pi", "/tmp", "sid-9") == {
        "calls": 2, "new_tokens": 1050, "cache_read": 9000, "out_tokens": 30}
    assert pi_cli.context_tokens("sid-9") == 5050


# --- the fence, in pi's own runtime language -----------------------------------

def _refused(tmp_path, command, deny):
    mod = tmp_path / "pi_fence.mts"           # node runs .mts as an ES module
    shutil.copy(pi_cli.FENCE, mod)
    js = (f"import {{ refused }} from {json.dumps(str(mod))};"
          f"console.log(JSON.stringify(refused({json.dumps(command)}, {json.dumps(deny)}) ?? null));")
    out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("command,blocked", [
    ("gh pr merge 12 --squash", "gh pr merge"),
    ("cd repo && gh pr review 5 --approve", "gh pr review"),
    ("git -C ../x push origin HEAD", "git push"),
    ("FOO=1 /usr/bin/gh -R o/r pr close 3", "gh pr close"),
    ("gh run watch 99", "gh run watch"),
    ("echo $(gh pr merge 1)", "gh pr merge"),
    ("gh pr view 12", None),
    ("git status && git diff", None),
    ("mvn -f service/pom.xml clean test", None),
])
def test_the_fence_refuses_his_decisions_in_any_spelling(tmp_path, command, blocked):
    from app import task_fence
    assert _refused(tmp_path, command, task_fence.denied("none")) == blocked


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_a_code_task_may_push_its_branch_but_never_merge(tmp_path):
    from app import task_fence
    deny = task_fence.denied("code")
    assert _refused(tmp_path, "git push -u origin feature/x", deny) is None
    assert _refused(tmp_path, "gh pr merge 3", deny) == "gh pr merge"


# --- impact, not only size ----------------------------------------------------------

def test_a_small_change_to_a_workflow_is_still_t3():
    tier, why = routing.plan_tier("STRUCTURE\nBookingWorkflowImpl.java\n"
                                  "Add Workflow.getVersion around the new signal.")
    assert tier == routing.T3 and "impact" in why


def test_a_small_ordinary_change_stays_small():
    assert routing.plan_tier("STRUCTURE\nEquipmentMapper.java\n")[0] == routing.T1


def test_pi_builds_on_the_same_claude_models():
    assert routing._cell("pi", routing.T3) == ("opus", "xhigh")
    assert pi_cli.model_id("sonnet") == "anthropic/claude-sonnet-5"
