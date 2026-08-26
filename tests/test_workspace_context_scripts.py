"""The context engine's own scripts, run for real against a throwaway workspace.

These are the JavaScript the whole context layer rests on: `check-drift.js` decides
what an agent is told is stale, and `resolve-task.js` decides which repo and which
files a task is routed to. Nothing in the Python suite covered them, which is how
three separate defects sat in them unnoticed while the workspace they serve drifted
286 commits.

Everything here builds a real git workspace in tmp_path and shells out to node, so
what is under test is the file that actually ships — not a Python re-implementation
of what it is believed to do.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RESOURCES = Path(__file__).resolve().parents[1] / "skills" / "workspace-context" / "resources"
CHECK_DRIFT = RESOURCES / "check-drift.js"
RESOLVE_TASK = RESOURCES / "resolve-task.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).stdout.strip()


def _repo(root: Path, key: str, files: dict[str, str]) -> str:
    """A real git repo with one commit. Returns its SHA."""
    d = root / key
    d.mkdir(parents=True, exist_ok=True)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t.t")
    _git(d, "config", "user.name", "t")
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "first")
    return _git(d, "rev-parse", "HEAD")


def _commit(root: Path, key: str, files: dict[str, str]) -> str:
    d = root / key
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "second")
    return _git(d, "rev-parse", "HEAD")


def _workspace(tmp_path: Path, ctx_name: str, repos: list[str]) -> Path:
    ctx = tmp_path / ctx_name
    (ctx / "repos").mkdir(parents=True)
    ctx.joinpath("workspace.yml").write_text(
        "workspace: test-ws\nversion: 3\nmode: workspace\nrepos:\n"
        + "".join(f"  - key: {r}\n    domains: [thing]\n    depends_on: []\n" for r in repos))
    return ctx


def _mini_skill(ctx: Path, repo: str, rel: str, sources: list[str]) -> None:
    p = ctx / "repos" / repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    src = "".join(f"  - {s}\n" for s in sources)
    p.write_text(f"---\nprimary_for: [thing]\nsources:\n{src}---\n\nSome fact.\n")


def _index(ctx: Path, repo: str, sha: str) -> None:
    d = ctx / "repos" / repo
    d.mkdir(parents=True, exist_ok=True)
    (d / "_index.json").write_text(
        json.dumps({"repo": repo, "verified_against": sha, "skills": []}))


def _run(script: Path, root: Path, *args: str, env: dict | None = None):
    return subprocess.run(["node", str(script), str(root), *args],
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


# --- the materiality gate ----------------------------------------------------

@pytest.fixture
def drifted(tmp_path):
    """A workspace whose repo has moved on, with a mix of real and inert changes."""
    ctx = _workspace(tmp_path, ".contmark", ["acme-billing-service"])
    first = _repo(tmp_path, "acme-billing-service",
                  {"src/main/java/Billing.java": "class Billing {}"})
    _mini_skill(ctx, "acme-billing-service", "runtime/billing-flow.md",
                ["src/main/java/Billing.java"])
    _index(ctx, "acme-billing-service", first)
    return tmp_path, ctx


def test_a_changed_source_file_makes_its_mini_skill_stale(drifted):
    root, _ = drifted
    _commit(root, "acme-billing-service", {"src/main/java/Billing.java": "class Billing { int x; }"})
    r = _run(CHECK_DRIFT, root, "--json")
    repo = json.loads(r.stdout)["repos"][0]
    assert repo["stale_mini_skills"] == ["runtime/billing-flow.md"]


def test_agent_tooling_is_not_reported_as_a_context_gap(drifted):
    """The real number that motivated this: 333 unclaimed files, 250 of them test
    fixtures and agent config. `skill-creator/LICENSE.txt` is not business
    knowledge, and a mini-skill enriched from it is worse than none."""
    root, _ = drifted
    _commit(root, "acme-billing-service", {
        ".agents/skills/skill-creator/LICENSE.txt": "MIT",
        ".claude/commands/fix-issue.md": "# fix",
        ".github/agents/thing.agent.md": "# agent",
        "src/test/java/BillingTest.java": "class BillingTest {}",
        "docs/how-to.md": "# doc",
    })
    repo = json.loads(_run(CHECK_DRIFT, root, "--json").stdout)["repos"][0]
    assert repo["unmatched_changed"] == []
    assert repo["unmatched_immaterial"] == 5


def test_real_source_still_gets_through_the_gate(drifted):
    root, _ = drifted
    _commit(root, "acme-billing-service", {
        "src/main/java/NewClient.java": "class NewClient {}",
        ".claude/settings.json": "{}",
    })
    repo = json.loads(_run(CHECK_DRIFT, root, "--json").stdout)["repos"][0]
    assert repo["unmatched_changed"] == ["src/main/java/NewClient.java"]
    assert repo["unmatched_immaterial"] == 1


def test_a_schema_survives_the_gate_though_it_looks_inert(drifted):
    """An .avsc is the shape of a contract, and the cross-repo graph is built from
    exactly these. Dropping one because it is not .java loses the edge."""
    root, _ = drifted
    _commit(root, "acme-billing-service", {
        "common/src/main/resources/avro/Booking.v2.avsc": '{"type":"record"}',
        "pom.xml": "<project/>",
    })
    got = json.loads(_run(CHECK_DRIFT, root, "--json").stdout)["repos"][0]["unmatched_changed"]
    assert sorted(got) == ["common/src/main/resources/avro/Booking.v2.avsc", "pom.xml"]


def test_a_contract_living_under_docs_is_rescued_from_the_gate(drifted):
    """The case the rescue list exists for. Plenty of services keep the API spec
    in docs/, and `docs/` is otherwise the most reliable noise signal there is —
    so the rule that drops the folder has to make an exception for the spec, or
    the gate quietly deletes the most load-bearing file in the repo."""
    root, _ = drifted
    _commit(root, "acme-billing-service", {
        "docs/openapi.yaml": "openapi: 3.0.0",
        "docs/how-we-work.md": "# team",
    })
    repo = json.loads(_run(CHECK_DRIFT, root, "--json").stdout)["repos"][0]
    assert repo["unmatched_changed"] == ["docs/openapi.yaml"]
    assert repo["unmatched_immaterial"] == 1


def test_noise_alone_does_not_invent_impacted_categories(drifted):
    """Before the gate, agent config classified into categories and sent a writer
    off to enrich `integrations/` from a settings file."""
    root, _ = drifted
    _commit(root, "acme-billing-service", {".agents/listener/consumer-notes.md": "x"})
    repo = json.loads(_run(CHECK_DRIFT, root, "--json").stdout)["repos"][0]
    assert repo["impacted_categories"] == []


def test_an_unchanged_repo_reports_no_drift(drifted):
    root, _ = drifted
    r = _run(CHECK_DRIFT, root, "--json")
    assert json.loads(r.stdout)["drift"] is False
    assert r.returncode == 0


def test_drift_exits_nonzero_so_ci_can_gate_on_it(drifted):
    root, _ = drifted
    _commit(root, "acme-billing-service", {"src/main/java/Billing.java": "class Billing { int y; }"})
    assert _run(CHECK_DRIFT, root, "--json").returncode == 1


# --- self-location -----------------------------------------------------------

def test_the_script_finds_the_context_dir_when_copied_into_it(drifted):
    """`node .contmark/check-drift.js .` must work. It used to default to
    `.asta-context` and report a perfectly bootstrapped workspace as not
    bootstrapped."""
    root, ctx = drifted
    shutil.copy2(CHECK_DRIFT, ctx / "check-drift.js")
    # Exactly how it is invoked in the field: from the workspace root, by the
    # path it was copied to. __dirname is then the context dir, which is the
    # whole point of self-location.
    r = subprocess.run(["node", f"{ctx.name}/check-drift.js", ".", "--json"],
                       cwd=str(root), capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "ASTA_CONTEXT_DIR"})
    assert json.loads(r.stdout)["repos"], r.stderr


def test_it_probes_for_the_layout_when_run_from_the_plugin(drifted):
    """No env, script outside the workspace: it must still find `.contmark`."""
    root, _ = drifted
    r = _run(CHECK_DRIFT, root, "--json",
             env={k: v for k, v in os.environ.items() if k != "ASTA_CONTEXT_DIR"})
    assert json.loads(r.stdout)["repos"]


def test_an_explicit_context_name_wins(tmp_path):
    """Asta passes the layout it detected; that must override any guessing."""
    ctx = _workspace(tmp_path, ".asta-context", ["acme-billing-service"])
    sha = _repo(tmp_path, "acme-billing-service", {"src/main/java/A.java": "class A {}"})
    _index(ctx, "acme-billing-service", sha)
    r = _run(CHECK_DRIFT, tmp_path, "--json", env={"ASTA_CONTEXT_DIR": ".asta-context"})
    assert json.loads(r.stdout)["repos"][0]["repo"] == "acme-billing-service"


def test_a_workspace_that_was_never_bootstrapped_says_so(tmp_path):
    r = _run(CHECK_DRIFT, tmp_path, "--json")
    assert r.returncode == 2
    assert "not_bootstrapped" in (r.stdout + r.stderr)


# --- the org prefix is derived, never hardcoded ------------------------------

def _routing_ws(tmp_path, keys, disambiguation=None):
    ctx = _workspace(tmp_path, ".contmark", keys)
    for k in keys:
        sha = _repo(tmp_path, k, {"src/main/java/A.java": "class A {}"})
        _index(ctx, k, sha)
        _mini_skill(ctx, k, "navigation/entry-points.md", ["src/main/java/A.java"])
    ctx.joinpath("_repo_router.json").write_text(json.dumps(
        {"schema_version": 2, "request_buckets": {}, "flows": [],
         "disambiguation_rules": disambiguation or [],
         "per_repo_summary": {k: k for k in keys}}))
    for name in ("_symbols.json", "_scenarios.json", "_global_index.json"):
        ctx.joinpath(name).write_text("{}")
    ctx.joinpath("_global_links.json").write_text("[]")
    return ctx


def test_a_shared_org_prefix_is_stripped_so_the_name_can_be_matched(tmp_path):
    """With a hardcoded placeholder in place, "acme-billing-service" normalised to
    "acmebillingservice", so a marker phrase of "billing" scored against the org
    name instead of the service. Derived from the keys, it is right for any org."""
    _routing_ws(tmp_path, ["acme-billing-service", "acme-shipping-service"])
    r = _run(RESOLVE_TASK, tmp_path, "billing charge calculation")
    assert r.returncode in (0, 3), r.stderr
    assert "acme-billing-service" in r.stdout


def test_repos_with_no_shared_prefix_lose_nothing(tmp_path):
    """Two unrelated keys must not have the first word of one stripped from both."""
    _routing_ws(tmp_path, ["billing-service", "payments-api"])
    r = _run(RESOLVE_TASK, tmp_path, "billing charge calculation")
    assert r.returncode in (0, 3), r.stderr
    assert "billing-service" in r.stdout


def test_a_single_repo_workspace_still_resolves(tmp_path):
    """N=1 is the degenerate case the whole engine claims to handle."""
    _routing_ws(tmp_path, ["lonely-service"])
    r = _run(RESOLVE_TASK, tmp_path, "anything at all")
    assert r.returncode in (0, 3), r.stderr
    assert "lonely-service" in r.stdout


def test_a_marker_naming_the_org_matches_no_single_repo(tmp_path):
    """The failure the derivation actually prevents, and it is silent.

    A disambiguation marker resolves its prefix against each repo key. Leave the
    org word in the key and "acme" is a substring of BOTH acme-billing-service and
    acme-shipping-service — so the loop keeps whichever it saw first and reports a
    confident answer to a genuinely ambiguous question. Strip the shared prefix and
    "acme" matches neither, so routing falls through to the honest path instead of
    inventing an owner.
    """
    _routing_ws(tmp_path, ["acme-billing-service", "acme-shipping-service"],
                disambiguation=[{"token": "charge",
                                 "acme_marker_phrases": ["charge calculation"]}])
    r = _run(RESOLVE_TASK, tmp_path, "charge calculation for the customer")
    assert r.returncode in (0, 3), r.stderr
    out = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
    trace = " ".join(out.get("trace", []))
    assert "disambiguation: marker" not in trace, (
        f"the org word resolved to a single owner: {trace}")
