// Asta's on-demand chat, for a turn that debugs a case (app/claude_cli.py,
// booking_case.investigating). Same as on-demand-chat, but the tools every
// investigation reaches for are in front from the first call: on 17 Nov each
// case turn spent its first calls searching for Bash, Read and Grep.
// MCP tools are left to their own server's placement (app/mcp_server.tool_meta).
const UP_FRONT = new Set(["ToolSearch", "Read", "Grep", "Glob", "Bash"]);

export const register = (on) => {
  on("tool.describe", ($, e, next) => {
    if (UP_FRONT.has(e.tool) || e.tool.startsWith("mcp__")) return next(e);
    return { description: e.description, isDeferred: true };
  });
};
