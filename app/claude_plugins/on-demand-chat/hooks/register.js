// Asta's on-demand chat (app/claude_cli.py, ASTA_ON_DEMAND): nothing is taken
// away. Each built-in tool stays listed by name and its schema loads the moment
// the model asks for it through ToolSearch — the one tool kept in front.
// MCP tools are left to their own server's placement (app/mcp_server.tool_meta).
const UP_FRONT = new Set(["ToolSearch"]);

export const register = (on) => {
  on("tool.describe", ($, e, next) => {
    if (UP_FRONT.has(e.tool) || e.tool.startsWith("mcp__")) return next(e);
    return { description: e.description, isDeferred: true };
  });
};
