"""Workspace registry + provider tests.

Every test points ASTA_WORKSPACES_FILE at a tmp file, so none of this can touch
the real registry in data/workspaces.json.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app import workspace as ws
from app.workspace import registry
from app.workspace.providers.indexed import IndexedProvider
from app.workspace.providers.plain import PlainProvider


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """An existing, empty registry.

    The file is pre-created on purpose: an ABSENT file is the one-time
    legacy-migration trigger (covered separately below), and letting that fire
    here would import whatever real workspace exists on the dev machine.
    """
    cfg = tmp_path / "workspaces.json"
    cfg.write_text("{}")
    monkeypatch.setenv("ASTA_WORKSPACES_FILE", str(cfg))
    yield


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    return path


@pytest.fixture
def plain_ws(tmp_path):
    root = tmp_path / "plain-workspace"
    _git_repo(root / "billing-service", {
        "src/Invoice.java": "class Invoice {\n  void computeVesselEta() {}\n}\n",
        "README.md": "billing\n",
    })
    _git_repo(root / "email-service", {
        "src/Mailer.java": "class Mailer {\n  void sendInvoice() {}\n}\n",
    })
    return root


@pytest.fixture
def indexed_ws(tmp_path):
    root = tmp_path / "indexed-workspace"
    (root / ".asta-context").mkdir(parents=True)
    (root / ".asta-context" / "resolve-task.js").write_text(
        "console.log('RESOLVED: ' + process.argv[3]);\n")
    (root / ".asta-context" / "check-drift.js").write_text("console.log('in sync');\n")
    (root / ".asta-context" / "lessons.md").write_text("Always run a clean build first.\n")
    (root / ".asta-context" / "boot.sh").write_text("echo boot\n")
    (root / ".asta-context" / "_global_index.json").write_text("{}")
    (root / ".asta-context" / "graph" / "_workspace").mkdir(parents=True)
    (root / ".asta-context" / "graph" / "_workspace" / "graph.html").write_text("<html></html>")
    _git_repo(root / "svc-a", {"a.txt": "a"})
    return root


# --- registry ----------------------------------------------------------------

def test_add_get_remove_roundtrip(plain_ws):
    w = ws.add("proj", plain_ws)
    assert w.name == "proj" and w.exists()
    assert ws.get("proj").root == str(plain_ws)
    assert "proj" in ws.names()
    assert ws.remove("proj") is True
    assert ws.get("proj") is None
    assert ws.remove("proj") is False


def test_registry_persists_to_disk(plain_ws, tmp_path):
    ws.add("proj", plain_ws, jira_projects=["abc"])
    raw = json.loads((tmp_path / "workspaces.json").read_text())
    assert raw["proj"]["root"] == str(plain_ws)
    assert raw["proj"]["jira_projects"] == ["ABC"], "keys are upper-cased on write"


def test_add_rejects_bad_name_and_missing_dir(plain_ws, tmp_path):
    with pytest.raises(ValueError):
        ws.add("Bad Name", plain_ws)
    with pytest.raises(ValueError):
        ws.add("ok", tmp_path / "does-not-exist")


def test_update_changes_fields(plain_ws):
    ws.add("proj", plain_ws)
    ws.update("proj", repos=["billing-service"], enabled=True)
    assert ws.list_services("proj") == ["billing-service"], "explicit repo list wins"


def test_unknown_workspace_names_the_registered_ones(plain_ws):
    ws.add("proj", plain_ws)
    with pytest.raises(ValueError, match="proj"):
        ws.read_workspace_file("nope", "x")


# --- provider detection ------------------------------------------------------

def test_detect_indexed_vs_plain(indexed_ws, plain_ws):
    assert registry.detect_provider(indexed_ws) == "indexed"
    assert registry.detect_provider(plain_ws) == "plain"
    assert IndexedProvider.detect(indexed_ws) is True
    assert IndexedProvider.detect(plain_ws) is False
    assert PlainProvider.detect(plain_ws) is True


def test_auto_provider_upgrades_when_index_appears(plain_ws):
    """A workspace registered as 'auto' must switch providers the moment an
    index shows up — no re-registration."""
    ws.add("proj", plain_ws)
    assert ws.provider_for("proj").id == "plain"
    (plain_ws / ".asta-context").mkdir()
    assert ws.provider_for("proj").id == "indexed"


def test_pinned_provider_is_not_auto_detected(indexed_ws):
    ws.add("proj", indexed_ws, provider="plain")
    assert ws.provider_for("proj").id == "plain"


def test_detect_reports_without_registering(plain_ws):
    info = ws.detect(plain_ws)
    assert info["ok"] and info["provider"] == "plain"
    assert set(info["repos"]) == {"billing-service", "email-service"}
    assert ws.names() == [], "detect must not register anything"


# --- auto-selection ----------------------------------------------------------

def test_infer_single_workspace_is_implicit(plain_ws):
    ws.add("only", plain_ws)
    assert ws.infer("fix the thing") == "only"


def test_infer_by_jira_key(plain_ws, indexed_ws):
    ws.add("alpha", plain_ws, jira_projects=["ALPHA"])
    ws.add("beta", indexed_ws, jira_projects=["BETA"])
    assert ws.infer("please look at BETA-1234") == "beta"
    assert ws.infer("ALPHA-1 is broken") == "alpha"


def test_infer_by_workspace_name_and_repo(plain_ws, indexed_ws):
    ws.add("alpha", plain_ws)
    ws.add("beta", indexed_ws)
    assert ws.infer("check the alpha workspace") == "alpha"
    assert ws.infer("what does billing-service do") == "alpha"
    assert ws.infer("", repo="svc-a") == "beta"


def test_infer_returns_none_when_ambiguous(plain_ws, indexed_ws):
    ws.add("alpha", plain_ws)
    ws.add("beta", indexed_ws)
    assert ws.infer("fix the bug") is None, "ambiguity must ask, not guess"


# --- file access -------------------------------------------------------------

def test_read_file_with_line_range(plain_ws):
    ws.add("proj", plain_ws)
    out = ws.read_workspace_file("proj", "billing-service/src/Invoice.java", 1, 2)
    assert out.startswith("1: class Invoice")
    assert "2:" in out and "3:" not in out


def test_read_file_refuses_path_escape(plain_ws):
    ws.add("proj", plain_ws)
    for bad in ("../../etc/passwd", "billing-service/../../../etc/passwd"):
        with pytest.raises(ValueError, match="escapes"):
            ws.read_workspace_file("proj", bad)


def test_list_services_skips_dotdirs_and_noise(plain_ws):
    (plain_ws / "node_modules").mkdir()
    (plain_ws / ".hidden").mkdir()
    ws.add("proj", plain_ws)
    assert ws.list_services("proj") == ["billing-service", "email-service"]


# --- plain provider ----------------------------------------------------------

def test_plain_resolve_ranks_matching_files(plain_ws):
    ws.add("proj", plain_ws)
    out = asyncio.run(ws.resolve_context("proj", "where is computeVesselEta defined"))
    assert "Invoice.java" in out
    assert "computeVesselEta" in out


def test_plain_resolve_reports_no_match(plain_ws):
    ws.add("proj", plain_ws)
    out = asyncio.run(ws.resolve_context("proj", "zzzzunfindabletoken"))
    assert "No matches" in out


def test_plain_provision_is_a_noop_that_explains_itself(plain_ws):
    ws.add("proj", plain_ws)
    out = asyncio.run(ws.provision("proj"))
    assert "No index built" in out and "billing-service" in out


def test_plain_status_admits_its_limits(plain_ws):
    ws.add("proj", plain_ws)
    assert "keyword search only" in ws.provider_for("proj").status()["note"]


# --- indexed provider --------------------------------------------------------

def test_indexed_resolve_shells_out_to_the_workspace_resolver(indexed_ws):
    ws.add("proj", indexed_ws)
    out = asyncio.run(ws.resolve_context("proj", "vessel eta"))
    assert "RESOLVED: vessel eta" in out


def test_indexed_conventions_reads_lessons(indexed_ws):
    ws.add("proj", indexed_ws)
    assert "clean build" in ws.conventions("proj")


def test_indexed_boot_command_quotes_the_hint(indexed_ws):
    ws.add("proj", indexed_ws)
    cmd = ws.boot_command("proj", 'vessel "eta" sync')
    assert cmd.startswith("sh .asta-context/boot.sh ")
    assert '"' not in cmd.split("boot.sh ", 1)[1].strip('"'), "inner quotes neutralised"


def test_indexed_drift_parses_resolver_output(indexed_ws):
    ws.add("proj", indexed_ws)
    stale, detail = asyncio.run(ws.drift("proj"))
    assert stale is False and "in sync" in detail

    (indexed_ws / ".asta-context" / "check-drift.js").write_text("console.log('DRIFT svc-a');\n")
    stale, detail = asyncio.run(ws.drift("proj"))
    assert stale is True and "DRIFT" in detail


def test_indexed_graph_pages(indexed_ws):
    ws.add("proj", indexed_ws)
    pages = ws.graph_pages("proj")
    assert pages == [{"name": "_workspace", "label": "Whole workspace",
                      "url": "/graph/proj/_workspace/graph.html"}]


def test_indexed_provision_stops_when_repos_lack_an_index(indexed_ws, monkeypatch, tmp_path):
    res = tmp_path / "res"
    res.mkdir()
    (res / "resolve-task.js").write_text("//")
    (res / "check-drift.js").write_text("//")
    monkeypatch.setenv("ASTA_CONTEXT_RESOURCES", str(res))
    ws.add("proj", indexed_ws)
    out = asyncio.run(ws.provision("proj"))
    assert "no per-repo index yet" in out
    assert "svc-a" in out


def test_indexed_provision_needs_resources_configured(indexed_ws, monkeypatch, tmp_path):
    monkeypatch.setenv("ASTA_CONTEXT_RESOURCES", str(tmp_path / "nope"))
    ws.add("proj", indexed_ws)
    assert "ASTA_CONTEXT_RESOURCES" in asyncio.run(ws.provision("proj"))


# --- back-compat -------------------------------------------------------------

def test_legacy_shim_still_works(plain_ws):
    from app import workspace_tools
    ws.add("proj", plain_ws)
    assert dict(workspace_tools.WORKSPACES) == {"proj": plain_ws}
    assert workspace_tools.list_services("proj") == ["billing-service", "email-service"]


def test_workspaces_view_is_live(plain_ws, indexed_ws):
    from app import workspace_tools
    assert len(workspace_tools.WORKSPACES) == 0
    ws.add("a", plain_ws)
    assert len(workspace_tools.WORKSPACES) == 1
    ws.add("b", indexed_ws)
    assert set(workspace_tools.WORKSPACES) == {"a", "b"}, "no restart needed"


def test_disabled_workspace_disappears_from_the_view(plain_ws):
    from app import workspace_tools
    ws.add("proj", plain_ws)
    ws.update("proj", enabled=False)
    assert dict(workspace_tools.WORKSPACES) == {}


def test_legacy_migration_runs_once_when_config_is_absent(tmp_path, monkeypatch):
    """Absent file => migrate. Present-but-empty file => never again, so removing
    your last workspace cannot resurrect the legacy one."""
    legacy = tmp_path / "booking-workspace"
    (legacy / "svc").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = tmp_path / "ws.json"
    monkeypatch.setenv("ASTA_WORKSPACES_FILE", str(cfg))

    assert "booking" in ws.all_workspaces(), "first read migrates"
    assert cfg.is_file(), "migration is persisted immediately"

    ws.remove("booking")
    assert ws.all_workspaces() == {}, "removal must stick"


def test_no_migration_when_there_is_nothing_to_migrate(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("ASTA_WORKSPACES_FILE", str(tmp_path / "ws.json"))
    assert ws.all_workspaces() == {}


# --- repo selection is a boundary, not a hint --------------------------------

def test_selected_repo_scopes_services_and_status(plain_ws):
    ws.add("proj", plain_ws, repos=["billing-service"])
    assert ws.list_services("proj") == ["billing-service"]
    assert ws.available_workspaces()["proj"]["repos"] == 1, "status counts the selection"


def test_selected_repo_scopes_search_results(plain_ws):
    """Selecting one repo must EXCLUDE the others from answers, not just from
    the listing — otherwise the user gets context they explicitly opted out of."""
    ws.add("all", plain_ws)
    both = asyncio.run(ws.resolve_context("all", "sendInvoice computeVesselEta"))
    assert "Invoice.java" in both and "Mailer.java" in both

    ws.add("one", plain_ws, repos=["billing-service"])
    scoped = asyncio.run(ws.resolve_context("one", "sendInvoice computeVesselEta"))
    assert "Invoice.java" in scoped
    assert "Mailer.java" not in scoped, "email-service was not selected"


def test_selection_of_a_nonexistent_repo_yields_nothing(plain_ws):
    ws.add("proj", plain_ws, repos=["ghost-service"])
    assert ws.list_services("proj") == []


def test_alternate_context_dirname_is_configurable(tmp_path, monkeypatch):
    """A workspace bootstrapped by another toolchain uses a different directory
    name. That name is configuration on this machine, never a constant in Asta."""
    root = tmp_path / "other-layout"
    (root / ".legacy-ctx").mkdir(parents=True)
    (root / ".legacy-ctx" / "resolve-task.js").write_text("console.log('hi');")
    assert registry.detect_provider(root) == "plain", "unknown layout: not indexed"

    monkeypatch.setenv("ASTA_CONTEXT_DIRNAMES", ".legacy-ctx")
    assert registry.detect_provider(root) == "indexed", "configured name is detected"

    ws.add("other", root)
    assert ws.provider_for("other").ctx.name == ".legacy-ctx"


def test_explicit_dirname_override_wins(tmp_path, monkeypatch):
    root = tmp_path / "pinned"
    (root / ".asta-context").mkdir(parents=True)
    (root / ".custom").mkdir()
    monkeypatch.setenv("ASTA_CONTEXT_DIRNAME", ".custom")
    ws.add("pinned", root)
    assert ws.provider_for("pinned").ctx.name == ".custom"
