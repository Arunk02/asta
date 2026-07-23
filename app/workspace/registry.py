"""The workspace registry — configuration, not code.

Replaces the hardcoded WORKSPACES dict that named one path in code.
Workspaces are registered at runtime, stored as JSON on the user's machine, and
resolved to a provider by detection rather than by name.

Auto-selection matters as much as configuration: the goal is that a user never
has to *say* which workspace they mean. A Jira key, a repo name, or simply
having exactly one workspace is enough. Only genuine ambiguity should reach the
user as a question.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .providers.base import ContextProvider
from .providers.indexed import IndexedProvider
from .providers.plain import PlainProvider

ROOT = Path(__file__).resolve().parent.parent.parent

#: order matters — the first provider that detects wins
PROVIDERS: tuple[type[ContextProvider], ...] = (IndexedProvider, PlainProvider)
_BY_ID = {p.id: p for p in PROVIDERS}


def config_path() -> Path:
    raw = os.environ.get("ASTA_WORKSPACES_FILE", "").strip()
    return Path(raw).expanduser() if raw else ROOT / "data" / "workspaces.json"


@dataclass
class Workspace:
    name: str
    root: str
    #: "auto" re-detects on every load, so a workspace upgrades itself the moment
    #: an index appears. Pin to a provider id only to override that.
    provider: str = "auto"
    #: [] means every repo directory under root
    repos: list[str] = field(default_factory=list)
    #: Jira project keys that imply this workspace (e.g. ["PROJ"])
    jira_projects: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def path(self) -> Path:
        return Path(self.root).expanduser()

    def exists(self) -> bool:
        return self.path.is_dir()


# --- persistence -------------------------------------------------------------

def _load_raw() -> dict:
    p = config_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def _save_raw(data: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def all_workspaces() -> dict[str, Workspace]:
    """Registered workspaces.

    On the very first read — config file absent — the legacy hardcoded location
    is migrated so an existing install keeps working with no action from the
    user. The file is then written unconditionally, even if migration found
    nothing, so this can never run twice. Without that, deleting your last
    workspace would silently resurrect the legacy one on the next read.
    """
    if not config_path().is_file():
        _save_raw(_migrate_legacy())
    raw = _load_raw()
    out = {}
    for name, cfg in raw.items():
        try:
            out[name] = Workspace(name=name, **cfg)
        except TypeError:
            continue
    return out


def _migrate_legacy() -> dict:
    """One-time upgrade from the pre-registry install, which had a single
    workspace whose path was hardcoded. The name is configuration, not a
    constant: ASTA_LEGACY_WORKSPACE overrides it, and a fresh install simply
    finds nothing and starts empty."""
    name = os.environ.get("ASTA_LEGACY_WORKSPACE", "booking-workspace").strip()
    legacy = Path.home() / name
    if not legacy.is_dir():
        return {}
    return {name.replace("-workspace", "") or name: {"root": str(legacy), "provider": "auto", "repos": [],
                        "jira_projects": [], "enabled": True}}


def get(name: str) -> Workspace | None:
    return all_workspaces().get(name)


def add(name: str, root: str | Path, *, provider: str = "auto",
        repos: list[str] | None = None, jira_projects: list[str] | None = None) -> Workspace:
    path = Path(root).expanduser()
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,31}", name or ""):
        raise ValueError("Name must be lowercase letters, digits, - or _ (max 32).")
    data = _load_raw()
    data[name] = {
        "root": str(path), "provider": provider,
        "repos": repos or [], "jira_projects": [j.upper() for j in (jira_projects or [])],
        "enabled": True,
    }
    _save_raw(data)
    return Workspace(name=name, **data[name])


def remove(name: str) -> bool:
    data = _load_raw()
    if name not in data:
        return False
    del data[name]
    _save_raw(data)
    return True


def update(name: str, **fields) -> Workspace:
    data = _load_raw()
    if name not in data:
        raise ValueError(f"Unknown workspace '{name}'")
    for k, v in fields.items():
        if k in ("root", "provider", "repos", "jira_projects", "enabled"):
            data[name][k] = v
    _save_raw(data)
    return Workspace(name=name, **data[name])


# --- provider resolution -----------------------------------------------------

def detect_provider(root: Path) -> str:
    for cls in PROVIDERS:
        try:
            if cls.detect(root):
                return cls.id
        except OSError:
            continue
    return PlainProvider.id


def provider_for(name: str) -> ContextProvider:
    ws = get(name)
    if ws is None:
        known = ", ".join(all_workspaces()) or "(none registered)"
        raise ValueError(f"Unknown workspace '{name}'. Registered: {known}")
    if not ws.exists():
        raise ValueError(f"Workspace '{name}' path is missing: {ws.root}")
    pid = ws.provider if ws.provider != "auto" else detect_provider(ws.path)
    cls = _BY_ID.get(pid, PlainProvider)
    return cls(ws.path, ws.repos)


# --- auto-selection ----------------------------------------------------------

_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,14})-\d+\b")


def infer(text: str = "", *, repo: str = "") -> str | None:
    """Best workspace for this request, or None when genuinely ambiguous.

    Deliberately conservative: guessing wrong sends a code task at the wrong
    repo. None means "ask", which is cheap; a wrong guess is not.
    """
    spaces = {n: w for n, w in all_workspaces().items() if w.enabled and w.exists()}
    if not spaces:
        return None
    if len(spaces) == 1:
        return next(iter(spaces))

    # 1. explicit workspace name in the text
    lowered = (text or "").lower()
    for name in spaces:
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return name

    # 2. Jira project key mapping
    for key in _JIRA_KEY.findall(text or ""):
        project = key.upper()
        for name, ws in spaces.items():
            if project in ws.jira_projects:
                return name

    # 3. a repo/service directory name
    needle = (repo or "").strip().lower()
    candidates = set()
    for name, ws in spaces.items():
        try:
            services = [p.name for p in ws.path.iterdir() if p.is_dir()]
        except OSError:
            continue
        for svc in services:
            if (needle and svc.lower() == needle) or \
               (not needle and re.search(rf"\b{re.escape(svc.lower())}\b", lowered)):
                candidates.add(name)
    return candidates.pop() if len(candidates) == 1 else None
