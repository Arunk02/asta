"""Back-compat shim. The workspace layer now lives in `app.workspace`.

Kept so existing callers (refresh, copilot_cli, claude_cli, token_audit, main)
keep working unchanged while they migrate. New code should import
`app.workspace` directly.
"""

from __future__ import annotations

from .workspace import (  # noqa: F401
    MAX_FILE_CHARS,
    WORKSPACES,
    available_workspaces,
    graph_pages,
    list_services,
    read_workspace_file,
    resolve_context,
)

__all__ = ["WORKSPACES", "MAX_FILE_CHARS", "available_workspaces", "resolve_context",
           "list_services", "read_workspace_file", "graph_pages"]
