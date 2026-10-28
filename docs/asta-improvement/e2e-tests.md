# End-to-end tests over real processes

`tests/e2e/` checks the work merged in #177–#184 across real process, network, git and file boundaries. Unit tests replace these boundaries with in-process fakes. Nothing here reaches GitHub, Teams, WhatsApp or a model:

- the API runs sealed (`app.sandbox`)
- the WhatsApp bridge points at a dead port
- GitHub is a local bare repository answered by a stand-in `gh` on `PATH`
- portals are local HTTP servers

| Suite | Real parts | What it proves |
|---|---|---|
| `test_worker_seam_e2e.py` | The Asta API in its own process; a real `python -m app.mcp_server` subprocess launched with exactly the environment a task gets, driven over MCP stdio | A worker sees only its scoped tools and only its own task. The owner token is never in its environment. Its token is refused on owner routes, on forged task ids, on write tools, after revocation and after the task ends. |
| `test_github_e2e.py` | A real bare git remote; `gh` run as an executable by Asta's own code; real squash merges | Task merges wait for CI on the *current* head and pin `--match-head-commit` to it, and the change really lands on `main`. Vinish is told once after GitHub reports the merge. Standalone "approve and merge if good" posts one approval on the real head and merges it. His own PR is never sent an approval. A push during review triggers a re-review. A plain review posts nothing. |
| `test_setup_portal_e2e.py` | A portal over real HTTP; every step a **separate OS process** (a real restart) | One access request across restarts. A real timeout after creation is reconciled, not resent. No setup before approval. Sign-in waits for him across a restart. An off-host redirect stops before any POST. A redesigned portal stops. Failed validation is not called done. The portal's secret never reaches the database or checkpoints. |
| `test_memory_e2e.py` | The API over HTTP owning a real memory folder and SQLite index | A busy project does not hide the asked one, and recall carries content. A correction replaces the fact in recall. Forget over HTTP is refused without his words. |

What these do **not** cover, because only live use can: real GitHub (permissions, branch protection), real Teams (chat resolution, delivery, the lobby wording), real model behaviour (whether a worker writes a good review or quotes the plan receipt), and the live Decision Hub portal.
