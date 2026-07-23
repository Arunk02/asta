# project context Context — Final Solution Architecture

*An AI-solution-architect view of the whole picture: where auto-generated code wikis (graphify,
Google Code Wiki / DeepWiki-class tools) fit, where they don't, and the one engine that owns it all.*

---

## 0. The one distinction that decides everything

There are **two different problems** that look like one. Almost every tool in this space solves
only the first and is then mis-sold as solving the second.

| | **Human comprehension** | **Agent context** |
|---|---|---|
| Reader | a person onboarding / reviewing | an LLM coding agent at Boot 0 |
| Need | "explain this repo to me" | "load the *exact* files for *this task*, nothing more" |
| Shape | prose + diagrams, browse top-down | routed file slices, every claim `(source: path:line)` |
| Cost model | read once, occasionally | read on **every** task — tokens are the budget |
| Drift tolerance | mild (a stale paragraph annoys) | fatal (a stale claim mis-routes the agent) |
| Won by | a wiki / graph | a **resolver over forensic mini-skills** |

**Code wikis — graphify, Google Code Wiki, DeepWiki, GitHub-style auto-docs — all live in the left
column.** They are human comprehension tools. They do not, and structurally cannot, be the agent
context layer, because they optimise for browse-ability, not for token-bounded per-task routing.

Everything below follows from this one split.

---

## 1. The landscape, placed correctly

| Tool / approach | Class | Source of truth | Who consumes it | Drift |
|---|---|---|---|---|
| **project context-workspace** (this) | agent context engine | forensic mini-skills + indexes | the **agent** via `resolve-task.js` | 3-layer ledger, self-healing |
| **Mermaid `diagrams.md`** (this, Step 3) | human view | the mini-skills (derived) | a **person** in IDE/GitHub | none — can't drift, it's generated |
| **graphify** (old code-wiki) | human graph | re-parses raw code (AST) | a **person** in a browser (D3) | drifts vs mini-skills |
| **Google Code Wiki** (codewiki.google) | hosted LLM wiki + Gemini Q&A chat | re-reads the repo, regenerated on every merge | a **person** on a website (private repos: waitlisted Gemini CLI) | auto-regenerated; separate source of truth from the mini-skills |

### What Google Code Wiki actually is (confirmed, Nov 2025 launch)

Public preview, real product — and it **validates this whole direction**, which is good news:

- **Auto-generated, always-current wiki** per repo; regenerates after each change / on every merged PR.
- **Auto architecture + class + sequence diagrams** that track the current code — exactly the Step-3
  Mermaid idea, proving the "diagrams derived from code, kept current" pattern is the right one.
- **Deep links** from every section/answer to the exact files/definitions.
- **Gemini-powered chat agent** that uses the wiki as context to answer questions about the repo.
- **Public preview = public GitHub repos only, hosted on Google's site.** Private/internal repos are a
  **Gemini CLI extension still in development (waitlist)** — not GA, no disclosed MCP server / API.

### Why it still doesn't replace the agent layer — sharpened by the real facts

Google building this **confirms the thesis**; it does not collapse the two columns. Four structural gaps remain:

1. **Its "agent" is a Q&A chat, not a pipeline router.** Code Wiki's Gemini agent *answers a human's
   question* conversationally. It is not a deterministic `resolve-task.js` returning
   `{route, matches[source:line], blast_radius}` to drive a build→test→review→PR pipeline. Different job.
2. **Separate, parallel source of truth → drift risk.** Code Wiki re-parses the repo itself; it is not
   *derived from* your mini-skills. So it can disagree with what the agent believes. The project context
   diagram layer is generated **from** the mini-skills, so it provably cannot.
3. **No programmatic contract (today).** No published MCP/API to wire into `project context.orchestrate` /
   `solo.*`. You can't make it the on-disk, token-bounded, `primary_for`/`mentions` context source.
4. **Hosted + public-only now; private = waitlist.** Private org repos can't use the hosted preview;
   the local CLI isn't GA. The workspace engine's value is context that's **on disk, git-tracked,
   diffable, gated in CI, drift-checked next to the code** — none of which a hosted wiki gives you.

So Google Code Wiki is the **best-in-class human enrichment** in this space — it *complements* the
engine (and outclasses graphify as the optional human view). It is not the agent context layer.

---

## 2. The final architecture — one skill, two layers, optional enrichment

```
                         project context-workspace   (ONE skill, all outputs)
                         ─────────────────────────────────────────────────
  MODE: single | workspace          TIER: lite | full
  $root = repo root (single)  |  workspace parent (workspace)

  ┌──────────────────────────── AGENT LAYER (the product) ────────────────────────────┐
  │  repos/<key>/  → 8-category forensic mini-skills, every claim (source: path:line)   │
  │  _global_index · _scenarios · _symbols · _repo_router · _global_links              │
  │  resolve-task.js   ← agents call this at Boot 0; reads indexes ON DISK (~350 tok)   │
  │  _drift.json + post-merge hook  ← 3-layer self-healing drift                        │
  └────────────────────────────────────────────────────────────────────────────────────┘
                                   │  derived from (single source of truth)
                                   ▼
  ┌──────────────────────────── HUMAN LAYER (free, derived) ──────────────────────────┐
  │  repos/<key>/navigation/diagrams.md   ← Mermaid flow/sequence/class, can't drift    │
  │  renders in any IDE / GitHub preview · every node → a real (source: …)              │
  └────────────────────────────────────────────────────────────────────────────────────┘
                                   │  optional, off by default
                                   ▼
  ┌──────────────────────── OPTIONAL ENRICHMENT (human only) ─────────────────────────┐
  │  graphify D3 graph.html   ·   or a hosted Google Code Wiki / DeepWiki link          │
  │  for big-repo exploration · NEVER wired into resolve-task.js · never agent context  │
  └────────────────────────────────────────────────────────────────────────────────────┘
```

**Three layers, one skill, one source of truth.** The mini-skills are authored once (forensic
bootstrap). The agent layer routes over them. The human layer is *generated from* them, so it can
never contradict what the agent believes. Hosted wikis / graphify sit outside as optional human
enrichment, never load-bearing.

---

## 3. How it actually works, end to end

```
SETUP (one-time, per repo; idempotent — skips bootstrapped repos)
  1 detect MODE         .git at cwd → single ($root=cwd) | ≥2 child .git → workspace
  2 forensic bootstrap  prompts 01/02 → 8-category mini-skills, (source: path:line), TIER-scaled
  3 diagram layer       generate diagrams.md FROM the mini-skills (human view, drift-proof)
  4 indexes + resolver  _global_index/_scenarios/_symbols; router+links (full | single-stub); copy resolve-task.js
  5 drift ledger        _drift.json + post-merge hook (Layer 1 no-AI, Layer 2 boot, Layer 3 evolution-loop)
  6 workspace.yml       + managed CLAUDE.md/AGENTS.md pointer (single: in repo · workspace: root only)

RUNTIME (every task — orchestrate / solo.claude / solo.copilot, Boot 0)
  1 walk up to .asta-context/workspace.yml   absent→legacy · mode:single→SINGLE · mode:workspace|absent→WORKSPACE
  2 resolve-task.js $root "<task>"  → {route, repo_order, matches[source:line], entry_files, blast_radius}
                                       indexes read ON DISK, never into context (~350 tok out)
  3 read lessons.md + _drift.json (Layer 2 stale check)
  4 FOR repo IN repo_order: cd workdir; load ONLY matched mini-skills @ source:line; run pipeline;
                            pass {workspace_context_dir, repo_context_dir} in every sub-agent payload
  5 WORKSPACE only: blast-radius reconciliation → append consumer repo (companion PR) if schema touched
```

The human never blocks the agent. Because `diagrams.md` is *derived from* the mini-skills, an agent
**may** read it — but it **costs tokens** (a file load, unlike the bounded resolver), so it is opt-in:
only for architecture / cross-system tasks, only when present. **graphify is a human-only view** — its
`graph.json` is 10–25 MB (un-loadable) and a second source of truth that re-parses code independently,
so agents do **not** consume it (a scoped `graphify path/explain` query tool is a possible future upgrade).

## Locked decision (this design)

- **One `.asta-context/` per workspace** holds everything: mini-skills, indexes, resolver, and — **only if
  the user opts in** — `diagrams.md` and `graph/`.
- **`diagrams.md` is OPTIONAL** (ask at setup, default off). Reading it costs the agent tokens, so it's
  never mandatory. When generated: one **whole-workspace** picture (all repos + external systems +
  cross-repo edges from `_global_links.json`) **plus** one per repo. Single mode = the one repo (N=1).
- **graphify is OPTIONAL + interactive**: after setup, ask _"want a graphify graph for visibility?"_ →
  user selects repos → install (macOS `uv tool install graphifyy` / Windows `pip install graphifyy`) →
  generate to `.asta-context/graph/<key>/`. 100% local, private-safe.
- **Neither is required for agents.** The agent works fully on `resolve-task.js` + mini-skills. When
  present, `diagrams.md` (small, derived, drift-proof) may be loaded for architecture tasks. **graphify
  is human-only** — agents never consume it (too large + drifting; future option = a `graphify
  path/explain` query tool, not file-load).

---

## 4. Decision matrix — when to use which human view

| Situation | Use | Why |
|---|---|---|
| Want a human review map / agent orientation | **Mermaid `diagrams.md`** (opt-in; whole-workspace + per-repo) | drift-proof, no dependency, renders in IDE/GitHub; agent-readable but **costs tokens** → opt-in |
| Very large repo, humans need to *explore/zoom* | **+ graphify** (opt-in, interactive) | local D3 beats static diagrams for sprawl; **human-only**, never agent context |
| Org wants a zero-setup hosted onboarding site + Q&A chat | **+ Google Code Wiki** (public repos now; private = waitlist CLI) | best-in-class human onboarding + Gemini chat; complements, never feeds the pipeline |
| Agent routing (every task) | **resolve-task.js + mini-skills** | the only layer that is token-bounded, addressable, drift-checked. Diagrams/graph are optional add-ons |

**Never** wire graphify `graph.json` or a hosted wiki into `resolve-task.js`. That would duplicate
`_symbols.json`/`_global_links.json` *without* the precision contract and add a second drift surface.

---

## 5. Final verdict

1. **One skill owns everything** — `project context-workspace` produces the agent layer, the human
   diagram layer, and exposes graphify as an opt-in. It supersedes `project context-workspace`,
   `project context-project-context`, and `project context-code-wiki`. No second skill needed.

2. **Code wikis (graphify, Google Code Wiki, DeepWiki) help humans, not agents.** They are excellent
   onboarding/review surfaces and worth offering as optional enrichment — but they are structurally
   the wrong layer for agent context: not per-task addressable, not token-bounded, no source-line
   precision contract, and (for hosted ones) cloud-opaque and un-diffable.

3. **The drift-proof human view is Mermaid `diagrams.md`**, generated from the mini-skills. It gives
   humans 90% of what a code wiki gives — for free, with zero dependency, and with a guarantee a
   hosted wiki can't make: it can never contradict the agent's truth, because it's derived from it.

4. **Agents stay on `resolve-task.js` + mini-skills** in both single and workspace mode. That layer
   is the product; everything else is a view of it or an optional extra around it.

> The mistake to avoid: treating "a beautiful auto-generated wiki" as the context strategy. It's a
> *view*. The strategy is forensic mini-skills + an on-disk resolver. Build the strategy once; render
> as many human views as you like on top — Mermaid by default, graphify or a hosted wiki when the
> repo's size justifies it.
