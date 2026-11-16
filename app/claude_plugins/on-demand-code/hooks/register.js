// Asta's on-demand code leg (app/claude_cli.py, ASTA_ON_DEMAND): the tools
// every build leg uses stay in front — 60 days of legs to 16 Nov were Bash
// 70%, Read 12%, Edit 9% — and every other built-in stays listed by name and
// loads the moment the leg asks for it through ToolSearch. Nothing is removed.
// MCP tools are left to their own server's placement (app/mcp_server.tool_meta).
const UP_FRONT = new Set(["Bash", "Read", "Edit", "Write", "ToolSearch"]);

export const register = (on) => {
  on("tool.describe", ($, e, next) => {
    if (UP_FRONT.has(e.tool) || e.tool.startsWith("mcp__")) return next(e);
    return { description: e.description, isDeferred: true };
  });
};
