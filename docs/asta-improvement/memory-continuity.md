# Preserve memory evidence

Nightly consolidation previously merged files sharing a 32-character filename prefix through an eight-line generated summary, deleted the second source, and pruned all but 30 episodes. These operations could remove distinct project facts and older decisions even though the prompt index already had its own size limit.

Consolidation now preserves all original facts and episodes, rebuilds the search index, and stores immutable content-addressed copies plus a path-to-version manifest under `memory/.history/`. Matching filenames never authorize a lossy merge. Historical copies remain outside automatic search to avoid retrieving the same evidence twice; the original notes stay searchable. The existing prompt index remains capped at 4,000 characters.

Episode digests use the full conversation/message fingerprint and retain earlier versions when a conversation gains new information. Digests record their conversation source and workspace. Repeating a digest for unchanged source messages reuses its existing file.

Project facts can record workspace and evidence source through the agent, HTTP and MCP tools. Task feedback records these automatically. Project-scoped recall omits memories known to belong to another workspace; legacy unscoped notes remain labelled as such. Recalled facts expose their source reference and date so the assistant can check evidence instead of treating the summary as current truth.

Regression coverage checks preservation of colliding titles, retrieval beyond 30 episodes, changed-conversation versions, workspace filtering, metadata injection, corrupt history detection, and HTTP capability parity. This change cannot restore notes already deleted by older versions; use an existing backup if recovery is needed. No live memory migration or deletion is performed by this PR.
