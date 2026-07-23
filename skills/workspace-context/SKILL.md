---
name: workspace-context
purpose: context-setup
description: >
  [Context Setup] ONE context engine for single- and multi-repo. A single repo is the N=1 case
  of a workspace: same forensic mini-skills, same on-disk `resolve-task.js` router, same agent
  Boot 0 — the cross-repo machinery just goes dormant. Two dials: MODE (single | workspace) and
  TIER (lite | full). Single mode writes a git-tracked `<repo>/.asta-context/`; workspace mode writes
  one `<workspace>/.asta-context/` over N repos. Generates a human Mermaid diagram layer FROM the
  mini-skills (single source of truth), on-demand drift detection (no hook/ledger), and a mode-aware pointer.
  Supersedes the legacy multi-only workspace skill, the lite tier (the lite tier), and
  the diagram layer (the diagram layer). Absent `.asta-context/` = not initialized — agents STOP and
  ask to run this skill first; there is no fallback context path.
---

# Context Engine — Single + Workspace, Lite + Full

One engine. A single repo is `N=1`; the resolver's precision file-loading is repo-count independent,
so a solo repo gets the same routed context a workspace does. The cross-repo indexes
(`_global_links`, blast radius, multi-entry router) degenerate cleanly at N=1 — empty, not special.

## The two dials

| Dial | Values | Decides |
|---|---|---|
| **MODE** | `single` \| `workspace` | Where `.asta-context/` lives + whether the repo loop runs |
| **TIER** | `lite` \| `full` | Bootstrap depth (probe at setup; per repo) |

- **single** — `cwd` is a git repo. `.asta-context/` at repo root, git-tracked. One repo entry.
- **workspace** — `cwd` is a parent of ≥2 cloned repos. One `<workspace>/.asta-context/`, repos stay clean.
- **lite** — small repo (few entry points, no external systems): `navigation/` + `stack/` +
  `architecture/modules.md` only. Resolver + `_scenarios.json` + `_symbols.json` still built.
- **full** — service with workflows/Kafka/REST/multiple capabilities: the forensic 8-category bootstrap.

Probe per repo: `lite` when source files < ~150 AND entry points ≤ 3 AND external systems = 0; else `full`.

## Outputs

```
SINGLE mode                              WORKSPACE mode
<repo>/.asta-context/                        <workspace>/.asta-context/
├── workspace.yml   (mode: single,       ├── workspace.yml   (mode: workspace, N repos)
│                    repos:[{root:"."}])  ├── _repo_router.json  _global_index.json
├── _repo_router.json (stub) _scenarios   ├── _global_links.json _scenarios.json _symbols.json
├── _global_links.json ([])  _global_idx  │
├── _symbols.json                          │
├── resolve-task.js  check-drift.js       ├── resolve-task.js  check-drift.js
├── diagrams.md (OPTIONAL, opt-in)       ├── diagrams.md (OPTIONAL, opt-in whole-workspace pic)
├── graph/      (OPTIONAL graphify)      ├── graph/      (OPTIONAL graphify, opt-in)
├── lessons.md                           ├── lessons.md
│  (_drift.json: OPTIONAL, CI-gate only)  │  (_drift.json: OPTIONAL, CI-gate only)
└── repos/<reponame>/                     └── repos/<key>/   (one per repo)
    ├── _index.json  _pins.yml                ├── _index.json  _pins.yml
    ├── domain/ architecture/ stack/          ├── domain/ architecture/ stack/ runtime/
    ├── runtime/ contracts/ integrations/     ├── contracts/ integrations/ operations/
    ├── operations/                           └── navigation/ (entry-points, scenarios,
    └── navigation/ (… + diagrams.md)             key-classes, diagrams.md)
```

Single mode still emits a **trivial** `_repo_router.json` (one repo; all buckets → that repo) and an
**empty** `_global_links.json` (`[]`) so `resolve-task.js` runs byte-identical — it simply finds one
repo and no cross-repo edges, short-circuiting to file-routing. No resolver change for N=1.

## Use when

- Onboarding any repo (single) or any directory of cloned repos (workspace) to project context.
- Adding a repo to an existing workspace, or re-detecting after the repo set changes.

## Do not use when

- Repos live under separate parents → workspace requires colocation; onboard each as `single`.

---

## Setup procedure

### Step 1 — Detect MODE

`.git` at `cwd` → **single** (`$root = cwd`). No `.git` at `cwd` but ≥2 subdirs hold `.git` →
**workspace** (`$root = cwd`). Neither → ask the user.

```bash
find . -mindepth 2 -maxdepth 2 -type d -name .git | sed 's|/\.git$||;s|^\./||'   # workspace candidates
```

Workspace: show candidates; allow deselection. Single: the one repo, no prompt.

### Step 2 — Per-repo bootstrap (forensic; TIER-scaled)

For each repo (`cd` in; single mode the repo IS `$root`). Skip if its `_index.json` exists.

```
execute resources/prompts/01-system-trace.prompt.md  → RAW SYSTEM TRACE
execute resources/prompts/02-context-skill.prompt.md → category blocks + _index.json block
```

**TIER=lite** — emit only `navigation/`, `stack/stack.md`, `architecture/modules.md`. Skip
runtime/contracts/integrations/operations. **TIER=full** — all 8 categories.

Write blocks to `<$root>/.asta-context/repos/<key>/<category>/<slug>.md`; the index to `_index.json`.
Replace `verified_against: HEAD` with `git rev-parse HEAD` (from the repo). Then validate:

```bash
node <plugin>/resources/validate-bootstrap.js <$root> --repo <key>   # exit 1 → re-run prompt 02 with failures
```

### Step 3 — Diagram layer (OPTIONAL; ask the user, then generate from the mini-skills)

**Ask:** _"Generate the Mermaid diagram layer (for human review + agent orientation)? (y/N)"_ Default
**no** → skip. Reading a diagram **costs the agent tokens** (a file load into context, unlike the
bounded resolver), so it is opt-in, never auto-produced. If yes, generate from the mini-skills +
`_global_links.json` (source of truth → can't drift). Two levels:

**A. Workspace whole-picture — `<$root>/.asta-context/diagrams.md` (always exactly one).** A single Mermaid
`flowchart` of the COMPLETE system: every repo as a `subgraph`, its entry points → core components, plus
**external systems** (Kafka topics, REST deps, DBs from `integrations/`) and **cross-repo edges** taken
from `_global_links.json` (producer→consumer, who-calls-whom). The "how it all connects" map. SINGLE
mode: the one repo + its external systems (same file, N=1).

**B. Per-repo detail — `repos/<key>/navigation/diagrams.md` (one per repo).** `flowchart` (entry points
→ components → stores/externals, from `navigation/entry-points.md` + `architecture/modules.md`), one
`sequenceDiagram` per top capability (from `runtime/*-flow.md`), `classDiagram` of 5–10 core types (from
`domain/`).

Every node maps to a real `(source: path:line)`. Cap ~20 nodes/diagram. When present, both levels are
regenerated whenever the underlying mini-skills change (evolution-loop, Step 5), so they stay in lock-step.

### Step 3b — graphify interactive layer (OPTIONAL; opt-in, HUMAN view only)

After the diagram layer, OFFER graphify for a richer interactive graph. Runs **100% locally** — code
never leaves the machine (private-safe). Flow:

1. **Ask:** _"Generate an interactive graphify graph for visibility? (y/N)"_ Default **no**; no → skip.
2. **Select repos:** list the repos; let the user pick a subset (default: all).
3. **Install** (only if `graphify` not on PATH) — print the command for the user's OS, wait for confirm:
   - **macOS:** `uv tool install graphifyy`  _(or `pip install graphifyy`)_
   - **Windows:** `pip install graphifyy`  _(or, with uv: `uv tool install graphifyy`)_
4. **Generate** per selected repo. graphify writes to `<repo>/graphify-out/` (NOT configurable — no
   `--out` flag), so relocate the 3 artifacts into the workspace and delete the rest to keep repos clean:
   ```bash
   ( cd <repo-path> && graphify update <repo-path> )          # tree-sitter extraction, no LLM
   mkdir -p <$root>/.asta-context/graph/<key>
   mv <repo-path>/graphify-out/{graph.json,graph.html,GRAPH_REPORT.md} <$root>/.asta-context/graph/<key>/
   rm -rf <repo-path>/graphify-out                            # repos stay clean (cache discarded)
   ```
5. **Whole-workspace graph** (optional): `graphify merge-graphs <$root>/.asta-context/graph/*/graph.json
   --out <$root>/.asta-context/graph/_workspace/graph.json` then `GRAPHIFY_VIZ_NODE_LIMIT=20000 graphify
   cluster-only <$root>/.asta-context/graph/_workspace --graph …/graph.json` (merge links repos by symbol
   overlap, NOT by the real cross-repo contracts — `diagrams.md` is the precise cross-system map).
   Note: repos >5000 nodes skip HTML unless `GRAPHIFY_VIZ_NODE_LIMIT` is raised.
6. **Humans open `graph.html`. Agents do NOT consume graphify** — `graph.json` is 10–25 MB (un-loadable)
   and a second, drifting source of truth. The agent's whole-system view is `diagrams.md` (small, derived,
   drift-proof). A scoped `graphify path`/`explain` query tool for blast-radius is a possible FUTURE
   upgrade — not wired today.

### Step 4 — Build indexes + resolver

- `_global_index.json` + `_scenarios.json` — `node <plugin>/resources/generate-indexes.js <$root> --write`
  (deterministic; flattens every `_index.json` + builds the scenario→path map). Run this **first**.
- `_symbols.json` — `node <plugin>/resources/generate-symbols.js <$root> --write` (reads `_global_index.json`).
- **`_repo_router.json` + `_global_links.json` — ALWAYS written** (the resolver requires both):
  - **workspace** — `_global_links.json` is DETERMINISTICALLY derived from source (STEP 6 of
    `generate-workspace.md`): `node <plugin>/resources/generate-links.js <$root> --write` then
    `node <plugin>/resources/validate-links.js <$root>` (exit 1 = a source call has no edge → fix).
    REST edges come from `*.base-url` host→repo resolution + client endpoint paths (never LLM
    name-matching, which silently missed edges). `_repo_router.json` via
    `resources/prompts/03-repo-router.prompt.md` (now also emits `glossary[]`) → `validate-router.js`;
    plus `routing_rules` + `depends_on` with cycle-break.
  - **single** — `_global_links.json` = `[]`; `_repo_router.json` = one-repo stub: `schema_version: 2`,
    every `request_buckets` phrase → the one repo, `flows: []`, `per_repo_summary` = the repo's summary,
    **`disambiguation_rules: []`** (array — the resolver iterates it with `for…of`; `{}` throws). Skip
    `validate-router.js` (no cross-repo flows).
- Copy `resources/resolve-task.js` **and** `resources/check-drift.js` to `<$root>/.asta-context/`.
- `node <plugin>/resources/validate-indexes.js <$root>` (exit 1 → regenerate offender).

### Step 5 — Drift detection (on-demand; no hook, no persistent ledger)

Detection rides on data already on disk: each mini-skill's `_index.json` carries `verified_against:<sha>`,
and each mini-skill `.md` carries its `sources:`. Nothing to install.

- **Detect** (agent Boot, no AI): `node <$root>/.asta-context/check-drift.js <$root>` → per repo, diffs
  `verified_against..HEAD` and maps each changed file to the **exact** stale mini-skill via its `sources:`
  (precise); category-classifies only unclaimed files (a possible new-code gap). exit 1 = drift.
- **Enrich** (the writer): hand the stale set to `the context enrichment pass`. It resolves the owning
  mini-skill — `node <plugin>/resources/resolve-skill-target.js <$root> --category <c> --keywords "…" [--repo <k>]`
  → `patch` an existing slug (≤10 lines) **or** `create` a new one for a new integration — then re-indexes:
  `generate-indexes.js` + `generate-symbols.js` + `reconcile-router.js` (all deterministic), and stamps
  `verified_against = HEAD` on the touched `_index.json`.
- **Cross-repo self-heal:** when a changed file is a client / `@KafkaListener` / config `*.base-url`,
  re-run `node <plugin>/resources/generate-links.js <$root> --write` + `validate-links.js` so a NEW
  cross-repo call becomes an edge automatically — links never go stale without a full re-setup.
- **No git hook, no `_drift.json`.** OPTIONAL: emit a ledger ONLY for a CI gate that fails a PR on drift
  without running an agent — `node check-drift.js <$root> --json > _drift.json`.

### Step 6 — `workspace.yml` + mode-aware pointer

Write `workspace.yml` (schema below). Then inject the managed pointer block
(`<!-- asta-context:start -->`…`:end`):

- **single** → into the repo's `CLAUDE.md`, `.github/copilot-instructions.md`, `AGENTS.md`.
- **workspace** → into a **workspace-root** `AGENTS.md` only; repos stay clean.

Pointer: "Context at `.asta-context/`. Agents route via `resolve-task.js` at Boot 0. Humans: see
`repos/<key>/navigation/diagrams.md`." Done.

---

## `workspace.yml` schema

```yaml
workspace: <name>
version: 3
mode: single            # single | workspace
tier: full              # default tier; per-repo override in repos[]
repos:
  - key: <reponame>
    root: "."           # single mode → "."; workspace mode → subdir name == key
    tier: full
    domains: [...]
    depends_on: []      # workspace only; a DAG (cycles broken → cycle_breaks)
# workspace mode also: routing_rules, cross_repo_contracts, cycle_breaks (see resources/workspace.yml.example)
```

---

## Agent contract — Boot 0 (single + workspace unified)

Asta's pipelines, Asta's pipelines, Asta's pipelines:

```
1. Walk up from cwd for .asta-context/workspace.yml.
     absent                       → NOT INITIALIZED — STOP. Print: "run the project context-workspace
                                    skill first (single repo = mode: single)". No fallback detection.
     mode: single                 → SINGLE.   $root = dir of .asta-context; the one repo's workdir = $root.
     mode: workspace (or absent)  → WORKSPACE. $root = dir of .asta-context; repos are subdirs.
                                    (absent mode = v2 workspace built by the old skill — back-compat.)

2. Build $resolve_text BEFORE resolving (the resolver needs signal NOUNS, not a bare ID/URL):
     classify input → jira | github | prompt; bind $mode + $ticket = FULL issue + comments
       (jira → getJiraIssue incl comments: added ACs/decisions/affected service are often ONLY in
       comments). $resolve_text = summary/title + AC titles + identifiers from body & comments
       (CamelCase, `code spans`, service/entity names); drop prose/repro/env/stack-traces.
       $ticket stays full (planning context, never trimmed); $resolve_text is the routing query only.
   node <$root>/.asta-context/resolve-task.js <$root> "$resolve_text"
     → ~350 tok: { route, repo_order, matches:[{repo,path,source?,line?}], entry_files, blast_radius,
       scores, glossary_hits:[{matched,canonical,values,owner_repos,source}] }
     The index files are read ON DISK — never enter context.
     GLOSSARY GROUNDING (router.glossary): maps ticket vocabulary → the codebase's REAL symbol +
       enum values + owner repos. Fixes routing misses on business words AND name drift (ticket
       "flow"/"service type" → canonical transportActivity(EXPORT|IMPORT)). Injects the canonical
       term for matching, seeds owner_repos as a recall net (route "glossary" when nothing else hit),
       and always emits glossary_hits. AGENTS: bind aliases to `canonical` — never invent a name.
     Routing is a PRECISION CASCADE (not frequency): symbol → flow → bucket → disambiguation →
       broad-token → glossary. symbol/flow/disambiguation resolve to one repo/chain (exact, short-circuit).
       bucket + broad-token are SCORED (phrases matched per repo; keep score ≥ (maxScore≥3 ? ceil(max/2)
       : 1)) to prune weakly-matched repos. Cross-repo expansion (contract must be NAMED — ≥2 distinct
       tokens or one long identifier): §7a upstream Kafka producer; §7a2 REST callee (a caller changing
       a named request contract PROMOTES the server/callee into repo_order — REST convention is
       producer=caller, consumers=callee); §7b Kafka downstream blast-radius stays advisory. NOT 100% —
       exact single-repo needs a symbol/identifier or disambiguation marker; else the relevant chain is returned.
     route=="ask" → widen ONCE (append body nouns), re-run; still ask → genuinely ambiguous.
     WORKSPACE + route=="ask" (exit 3) → print candidates, STOP (user picks the repo).
     SINGLE: repo_order = the one repo; blast_radius = []. route=="ask" → do NOT prompt (only one
       repo); load that repo's navigation/ (entry-points.md + scenarios.md) and proceed.

2b. OPTIONAL (only if present): for architecture / cross-system tasks, may load
     <$root>/.asta-context/diagrams.md (small, derived from mini-skills → safe; nodes carry source:line);
     skip silently if absent. graphify is a HUMAN view only — never agent context (graph.json is
     10–25 MB / un-loadable + a drifting second source; a scoped `graphify path/explain` query tool
     is a possible FUTURE upgrade, not wired).

3. Read <$root>/.asta-context/lessons.md → $workspace_lessons. Run `node <$root>/.asta-context/check-drift.js
     <$root>` (exit 1 = drift) → report stale mini-skills; hand the stale set to evolution-loop.

4. FOR $repo IN repo_order:
     workdir = (SINGLE ? $root : <$root>/<$repo>);  cd workdir
     $workspace_context_dir = <$root>/.asta-context
     $repo_context_dir      = <$root>/.asta-context/repos/<$repo>
     load ONLY $matches mini-skills (open at source:line); read _pins.yml → $skills.*
     run the pipeline; pass both dirs in every sub-agent payload
     WORKSPACE: capture pr_url+commit_sha → previous_repos for the next iteration

4b. IMPACT — analyse BOTH directions across the workspace, never stop at one repo. `repo_order` =
     core + upstream (parent/source/producer, added proactively via §7a/§7a2). `blast_radius` =
     downstream consumers of a NAMED contract (§7b, contract-gated). Discovery gate code-verifies
     each direction at source:line and promotes any genuinely-impacted repo into scope. Plan.md
     §Interpretation & Impact highlights every in-scope repo (direction + file:line) + every
     term→symbol binding for the USER to confirm before approval.

4c. GLOSSARY LEARNING — plan-gate feedback that corrects a term/acronym mapping is persisted (confirmed
     + code-verified only) to `_repo_router.json` glossary[] so future tasks resolve it automatically.
     This is the ONE index an agent may write, and only on explicit user confirmation.

5. WORKSPACE only — blast-radius reconciliation: producer diff touched schema_path/serialization?
     YES → append consumer to repo_order (companion PR). NO → Reviewer notes verified-unaffected.
```

Sub-agents never detect MODE — they read `cwd` + the two payload dirs. **Zero sub-agent change vs the
existing pipeline** — the dual-dir payload contract is already in place.

---

## Migration / supersession

| Replaces | Becomes |
|---|---|
| legacy multi-only workspace skill | this skill in `mode: workspace` |
| `the lite tier` | the **lite tier** + the diagram layer |
| `the diagram layer` | the **diagram layer** (Step 3), regenerated from mini-skills so it can't drift |

graphify (code-wiki) stays **optional** enrichment only; never a hard dependency.

## Rules

- Setup is one-time; re-running skips bootstrapped repos.
- Agents never modify `workspace.yml` or any index — hand-edit only. SOLE exception: `_repo_router.json` `glossary[]`, updated by the Planner ONLY on explicit user confirmation of a term/acronym mapping at the plan gate (confirmed + code-verified `source`), so acronyms are learned for future tasks.
- `cwd` is the single source of truth for which repo a sub-agent is in.
- Diagrams + graphify are BOTH OPTIONAL — ask the user at setup; default off. When generated, diagrams
  come from mini-skills only (no drift): one whole-workspace `diagrams.md` + one per repo; agents may
  load `diagrams.md` only when useful, skip if absent. **graphify is a HUMAN-only view** (local, large,
  separate source of truth) — agents do not consume it. The agent context layer is `resolve-task.js` +
  mini-skills, full stop.
- `depends_on` is a DAG; a cycle is broken (back-edge → `cycle_breaks`), warned, never aborted.
- No `CONSTITUTION.md`; no `[INLINE]`/`[SPLIT]`; no 200-token cap. Hard cap 150 lines per mini-skill.
- Single mode commits `.asta-context/`; workspace mode never writes inside any repo.
- `primary_for` vs `mentions` is the precision contract. Honour it.
