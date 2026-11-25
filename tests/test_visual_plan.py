"""Round 5, PR D: the plan reaches him as a PDF with its flows drawn, never as a
.md, and — when he asks — goes up as a draft PR for the team first.

His words, 25 Nov: "diagrammatic PDF … easy to read and understand just by
watching, remove sending the plan.md in the message"; and for big features
"create the plan, raise a PR, let others approve or comment, then implement"."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app import plan_visual, store, tasks

PLAN = """STRUCTURE
  RailEtaValidator.validate()   new check, mirrors the vessel one
  CamsRetryChildWorkflowImpl    calls it before the retry

FLOW
```mermaid
flowchart TD
  A["CAMS rail ETA"] --> B["RailEtaValidator (new)"] --> C["retry child workflow"]
```

RISK: none — covered by a new test
"""


def test_mermaid_blocks_become_diagrams():
    html = plan_visual.page(PLAN, "Plan for task 9")
    assert '<pre class="mermaid">' in html and "flowchart TD" in html
    assert "```" not in html


def test_a_plan_without_a_diagram_gets_one_from_its_structure():
    plain = "STRUCTURE\n  RailEtaValidator.validate()   new check\n  CamsRetry   calls it\n\nRISK: none\n"
    drawn = plan_visual.structure_diagram(plain)
    assert drawn.startswith("flowchart TD") and "RailEtaValidator" in drawn and "n0 --> n1" in drawn
    assert '<pre class="mermaid">' in plan_visual.page(plain, "t")


def test_labels_cannot_break_the_diagram():
    assert '"' not in plan_visual._label('say "hi" [x] {y} | z')


def test_switched_off_it_renders_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTA_PLAN_VISUAL", "0")
    md = tmp_path / "plan-1.md"
    md.write_text("# Plan\n\n" + PLAN)
    assert asyncio.run(plan_visual.render(md)) is None


@pytest.mark.skipif(not plan_visual.chrome(), reason="needs a local Chrome")
def test_a_real_render_draws_the_diagram(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTA_PLAN_VISUAL", "1")
    md = tmp_path / "plan-9.md"
    md.write_text("# Plan for task #9 — rail ETA\n\n" + PLAN)
    out = asyncio.run(plan_visual.render(md, "rail ETA"))
    if out is None and plan_visual.mermaid_js() is None:
        pytest.skip("Mermaid could not be fetched (offline)")
    assert out is not None and out.suffix == ".pdf" and out.stat().st_size > 2000


def test_a_tampered_mermaid_copy_is_never_used(monkeypatch, tmp_path):
    monkeypatch.setattr(plan_visual, "_vendor_dir", lambda: tmp_path)
    (tmp_path / "mermaid-10.9.1.min.js").write_text("alert('not mermaid')")

    class Bad:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"also not mermaid"
    monkeypatch.setattr(plan_visual.urllib.request, "urlopen", lambda *a, **k: Bad())
    assert plan_visual.mermaid_js() is None


# --- the card ------------------------------------------------------------------------

def test_with_the_pdf_attached_the_card_carries_no_plan_text():
    assert tasks._card_text(PLAN, "📎 The whole plan is attached as plan-9.pdf (12 lines).\n") == ""


def test_without_a_pdf_the_card_carries_the_plan():
    assert "RailEtaValidator" in tasks._card_text(PLAN, "")


def test_the_md_is_never_sent(monkeypatch):
    from app import notify, plan_pdf
    sent = []

    async def doc(path, caption=""):
        sent.append(path)
        return True

    async def no_visual(*a, **k):
        return None
    monkeypatch.setattr(notify, "wa_document", doc)
    monkeypatch.setattr(plan_visual, "render", no_visual)
    monkeypatch.setattr(plan_pdf, "render", lambda *a, **k: None)
    t = store.create_task("rail ETA", "code", "p", None)
    line = asyncio.run(tasks._send_full_plan(t["id"], t, PLAN))
    assert sent == [] and line == ""


# --- the plan PR ---------------------------------------------------------------------

@pytest.mark.parametrize("text", ["raise a plan PR", "share the plan as a PR first",
                                  "plan PR please", "open plan pull request for the team"])
def test_he_can_ask_for_a_plan_pr(text):
    assert tasks.wants_plan_pr(text)


@pytest.mark.parametrize("text", ["approve task 9", "raise PR", "change the plan to use a map"])
def test_other_answers_are_not_a_plan_pr(text):
    assert not tasks.wants_plan_pr(text)


def _repo_with_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(remote)], check=True)
    repo = tmp_path / "checkout" / "svc"
    repo.parent.mkdir()
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a], check=True, capture_output=True)
    (repo / "README.md").write_text("svc\n")
    g("add", "-A"); g("commit", "-qm", "base"); g("push", "-q", "origin", "HEAD:develop")
    g("fetch", "-q"); g("switch", "-q", "-c", "ABC-9-rail-eta")
    # Asta's own commit in ship_plan uses the repo's identity; CI has no global one.
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    return repo


def test_the_plan_goes_up_as_a_draft_pr_on_the_task_branch(monkeypatch, tmp_path):
    from app import repo_ops
    repo = _repo_with_remote(tmp_path)
    t = store.create_task("ABC-9 rail ETA validation", "code", "ABC-9 add it", None)
    tid = t["id"]
    store.kv_set(f"task_gate:{tid}", "plan")
    store.update_task(tid, status="awaiting_approval", result=PLAN)
    gh_calls = []
    real_git = repo_ops.git

    async def git(cwd, *args, **kw):
        if args and args[0] == "gh":
            gh_calls.append(args)
            return 0, ""
        return await real_git(cwd, *args, **kw)

    async def nothing(*a, **k):
        return None

    async def verified(repo_, head, base=""):
        return "https://example.test/org/svc/pull/7"
    monkeypatch.setattr(repo_ops, "git", git)
    monkeypatch.setattr(tasks, "ensure_checkout", nothing)
    monkeypatch.setattr(tasks, "task_cwd", lambda *a, **k: str(repo))
    monkeypatch.setattr(tasks, "_verified_pr", verified)
    url = asyncio.run(tasks.ship_plan(tid))
    assert url.endswith("/pull/7")
    doc = repo / "docs" / "specs" / "ABC-9-rail-eta-plan.md"
    assert doc.is_file() and "```mermaid" in doc.read_text() and "Ticket: ABC-9" in doc.read_text()
    pushed = subprocess.run(["git", "-C", str(repo), "ls-remote", "origin", "ABC-9-rail-eta"],
                            capture_output=True, text=True).stdout
    assert pushed.strip()
    create = [c for c in gh_calls if c[:3] == ("gh", "pr", "create")][0]
    assert "--draft" in create and "Plan: ABC-9 rail ETA validation" in create
    info = tasks.plan_pr(tid)
    assert info["path"] == "docs/specs/ABC-9-rail-eta-plan.md" and info["branch"] == "ABC-9-rail-eta"
    # Still waiting for HIS approve: a plan PR is not an approval.
    assert store.get_task(tid)["status"] == "awaiting_approval"
    # A revised plan at the gate follows onto the same PR.
    assert asyncio.run(tasks.refresh_plan_pr(tid, PLAN + "\nRISK: one more\n"))
    assert "one more" in doc.read_text()
    # The build is told the plan commit holds no code.
    approved, brief = tasks.record_answer(tid, store.get_task(tid), "approve task %d" % tid)
    assert approved and "No code is done yet" in brief


def test_a_plan_pr_needs_the_plan_gate(monkeypatch):
    t = store.create_task("x", "code", "p", None)
    store.update_task(t["id"], status="shipped")
    with pytest.raises(ValueError):
        asyncio.run(tasks.ship_plan(t["id"]))
