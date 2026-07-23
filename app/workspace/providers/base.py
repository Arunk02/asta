"""What Asta needs from a workspace, stated once.

A *context provider* answers questions about a codebase without Asta knowing how
the answers are produced. Today one provider shells out to a context-indexing
toolchain that lives in the user's workspace; another just uses git and ripgrep.
Neither name appears anywhere else in Asta.

The split that matters:

  Asta's repo owns the PIPELINE  — how work is staged, gated and narrated. It is
                                   generic, ships with Asta, and is identical for
                                   every user (see `agents/`).
  The workspace owns the FACTS   — which repos exist, which files answer a task,
                                   what the build command is, what was learned
                                   here. Generated on the user's machine, never
                                   uploaded.

That boundary is why a provider never returns prompts or instructions, only
data — anything instruction-shaped coming out of a user's workspace is untrusted
input and is wrapped as such by the caller.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class ContextProvider(ABC):
    """Read-only view of one workspace's project context."""

    #: stable identifier persisted in the registry; also the name users see
    id: str = "base"
    #: shown in setup output
    label: str = "Base"

    def __init__(self, root: Path, repos: list[str] | None = None) -> None:
        self.root = Path(root)
        #: repo subset the user selected; empty/None means every repo under root.
        #: Every provider must honour this — a user who selected one repo must
        #: not get answers from the others.
        self.repos = list(repos or [])

    # --- discovery -----------------------------------------------------------

    @classmethod
    @abstractmethod
    def detect(cls, root: Path) -> bool:
        """Whether this provider can serve `root` as it stands right now."""

    @abstractmethod
    def status(self) -> dict:
        """Health/coverage summary for the UI and `asta workspace list`."""

    # --- the questions Asta actually asks ------------------------------------

    @abstractmethod
    async def resolve(self, task: str) -> str:
        """Which files/lines answer this task. The whole point of a provider:
        surgical context instead of letting an agent explore a repo blindly."""

    def services(self) -> list[str]:
        """Repo directories inside the workspace. Path-shaped, so it has a
        sensible default every provider can inherit."""
        if not self.root.is_dir():
            return []
        found = sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name not in ("npmcache", "node_modules")
        )
        if self.repos:
            return [r for r in found if r in self.repos]
        return found

    def conventions(self) -> str:
        """Build/test commands, hard-won lessons, pinned facts — the things that
        make a change land correctly *here*. Empty string when unknown."""
        return ""

    def boot_command(self, hint: str = "") -> str | None:
        """A single shell command that loads all orientation context at once, if
        the workspace provides one. Collapsing boot to one call was worth ~17% of
        a planning run, so it is asked for explicitly rather than rediscovered."""
        return None

    async def drift(self) -> tuple[bool, str]:
        """(is_stale, detail). Detection must be deterministic and free — the
        costly re-indexing is a separate, explicitly-approved step."""
        return False, ""

    def graph_pages(self, workspace_name: str) -> list[dict]:
        """Pre-rendered architecture pages for the UI, if any."""
        return []

    # --- lifecycle -----------------------------------------------------------

    async def provision(self, repos: list[str] | None = None) -> str:
        """One-time setup: generate this workspace's context on the user's
        machine. `repos` restricts which repos participate (None = all)."""
        return f"{self.label}: nothing to provision."

    async def enrich(self) -> str:
        """Bring an existing context up to date after code changed. Kept
        separate from `drift` because this one can be expensive."""
        return f"{self.label}: nothing to enrich."
