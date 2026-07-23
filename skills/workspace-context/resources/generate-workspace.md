## Role:

Execution orchestrator for the one-time multi-repo workspace bootstrap. Builds a single workspace-level `.asta-context/` tree containing forensic mini-skills for every selected repo plus four workspace indexes that drive precision routing.

## Objective:

Produce, in a single pass, under `<workspace>/.asta-context/`:

1. `repos/<repo-key>/` per repo — eight category folders of forensic mini-skill files + `_index.json` + `_pins.yml`.
2. `_global_index.json` — concatenated per-repo indexes with `repo` prepended; an agent filters by `repo ∈ $impacted_repos` before scanning.
3. `_global_links.json` — cross-repo edges derived from `integrations/*` + `contracts/kafka-events.md`.
4. `_scenarios.json` — task-phrase → `[{repo, path}]` file-level lookup table.
5. **`_repo_router.json`** — TOP-LEVEL agent router with `request_buckets` + `flows` + `per_repo_summary` + `disambiguation_rules`. Built by `prompts/03-repo-router.prompt.md`. Agents read this FIRST.
6. `workspace.yml` — router (repos, depends_on, routing_rules, cross_repo_contracts).
7. `lessons.md` — empty stub.
8. `hooks/post-merge-context-drift.sh` — drift detector at workspace level.

No `CONSTITUTION.md`. No `§1`–`§7`. No `[INLINE]`/`[SPLIT]` markers. No 200-token cap. No per-repo `.asta-context/`.

## Context:

Bundled under `resources/`:

- `prompts/01-system-trace.prompt.md` — forensic per-repo execution trace.
- `prompts/02-context-skill.prompt.md` — partitioner; emits per-file blocks + `_index.json` block with `primary_for` / `mentions` / `scenarios` routing fields.
- `prompts/03-repo-router.prompt.md` — builds `_repo_router.json` from all per-repo indexes.
- `templates/category-file.md`, `templates/_index.json.example`, `templates/_pins.yml.example`.
- `hooks/post-merge-context-drift.sh` — per-file drift detector that also regenerates `_index.json`.

Downstream agent contract is documented in `SKILL.md` § "Agent contract (Boot 0)".

## Instructions:

### STEP 1 — Detect workspace root

`cwd` is the workspace root. If `.git` exists at `cwd` → STOP: _"This is a repository, not a workspace. Use project-context directly."_

### STEP 2 — Discover candidate repos (the ONLY user prompt)

```bash
find . -mindepth 2 -maxdepth 2 -type d -name .git | sed 's|/\.git$||;s|^\./||'
```

Present list. Ask which to register. **This is the only user prompt in the entire flow.** Steps 3–8 derive everything from code and from the artifacts STEP 3 generates — never ask.

### STEP 3 — Per-repo forensic bootstrap

`mkdir -p <workspace>/.asta-context/repos`

For each registered repo:

```
cd <workspace>/<repo>

if <workspace>/.asta-context/repos/<repo>/_index.json exists:
  skip (idempotent — re-run after a code change uses STEP 3-refresh)
else:
  ── Trace ───────────────────────────────────────
  execute resources/prompts/01-system-trace.prompt.md → RAW SYSTEM TRACE
  if trace incomplete → re-run once on missed areas

  ── Partition ───────────────────────────────────
  execute resources/prompts/02-context-skill.prompt.md
  → emits FILE blocks ending with one INDEX block.

  Forensic-depth contract: the partitioner MUST emit (when the trace has the material):
    - runtime/event-activity-matrix.md   (whenever a workflow engine drives event → activity sequences)
    - navigation/scenarios.md             (task-phrase → start-here class.method)
    - operations/failure-model.md         (failure-point / cause / symptom table)
    - operations/flags-and-lists.md       (env flags + country/channel lists)
    - architecture/modules.md             (full package tree)
  Every flow file MUST contain a "Variant Routing" table mapping trigger condition → resolver class.
  Repo has a mapping layer (MapStruct `@Mapper` / `*Mapper` classes) → every flow file MUST contain a
  "Mapping Chain" table: every mapper the flow crosses, in hop order. An omitted hop is a silent
  field drop (unmapped fields compile green).
  Hard cap 150 lines per file.

  ── Scaffold ────────────────────────────────────
  HEAD_SHA = git rev-parse HEAD     # run from <repo>, not workspace
  TS       = ISO now

  mkdir -p <workspace>/.asta-context/repos/<repo>/{domain,architecture,stack,runtime,contracts,integrations,operations,navigation}
  prune empty categories.

  for each FILE block:
    target = <workspace>/.asta-context/repos/<repo>/<category>/<slug>.md
    write block body to target
    replace `verified_against: HEAD` with HEAD_SHA
    set last_updated to TS

  write INDEX block body to <workspace>/.asta-context/repos/<repo>/_index.json
  replace `verified_against: HEAD` with HEAD_SHA
  set generated_at to TS

  ── Pins ────────────────────────────────────────
  write _pins.yml to <workspace>/.asta-context/repos/<repo>/_pins.yml from templates/_pins.yml.example
  pre-fill asta_skills.stack + asta_skills.build using stack/stack.md
  VALIDATE every asta_skills entry against the installed set: list
  <workspace>/.github/skills/ and <workspace>/.claude/skills/; keep an entry only
  if `project context-<entry>/SKILL.md` exists there. No match → drop it (empty group is
  fine). Never invent stack variants — `unit-testing-kotlin` is wrong when only
  `unit-testing-java` is installed; a dead name is a wasted boot read.

cd <workspace>
```

**Do NOT** write anything inside `<repo>/.asta-context/`. **Do NOT** modify the repo's `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`, or `.cursor/rules/`. Repos stay clean.

### STEP 3-refresh — Re-running on an existing repo

If `<workspace>/.asta-context/repos/<repo>/_index.json` exists but a refresh is needed, accept `--category <name>` or `--file <path>` flags:

```
--category runtime    → re-trace + re-partition only runtime/* files
--file <path>         → re-trace + re-partition that one file
```

### STEP 4 — Auto-classify each repo

No prompts. Derive `domains` and `depends_on` from artifacts STEP 3 generated + repo build files.

```
FOR repo IN registered_repos:
  domains = sort(unique(flatten(entry.domains for entry in <workspace>/.asta-context/repos/<repo>/_index.json.files)))

  depends_on = []
  # signal 1 — build manifests
  for f in [pom.xml, build.gradle, build.gradle.kts, package.json, pyproject.toml, go.mod, Cargo.toml]:
    parse coordinates;
    for other in registered_repos - {repo}:
      if other.key matches any coordinate (case-insensitive; - and _ equal):
        depends_on += other.key

  # signal 2 — shared-schema imports
  grep <repo> src for 'import|include' referencing another repo's
    proto/ avro/ openapi/ schemas/ contracts/ → add edge

  # signal 3 — integration mini-skills (peer_systems front matter)
  for f in <workspace>/.asta-context/repos/<repo>/integrations/*.md:
    parse f.front_matter.peer_systems → for each slug, if matches another key: depends_on += that key

  # signal 4 — kafka pairing
  parse <workspace>/.asta-context/repos/<repo>/contracts/kafka-events.md → produced/consumed topics
  for other in registered_repos - {repo}:
    if any topic in repo.consumed appears in other.produced: depends_on += other.key

  depends_on = sort(unique(depends_on))

build DAG; if cycle → ABORT "WORKSPACE_CYCLE: <chain>"
```

### STEP 5 — Build `_global_index.json`

```
out = { generated_at: ISO_NOW, files: [] }
FOR repo IN registered_repos:
  idx = read <workspace>/.asta-context/repos/<repo>/_index.json
  FOR entry IN idx.files:
    entry.repo = repo
    out.files.append(entry)
write out → <workspace>/.asta-context/_global_index.json
```

### STEP 6 — Build `_global_links.json` — DETERMINISTIC, from source

Do NOT hand-author or name-match edges (the old approach silently missed any REST edge whose peer
didn't string-match a repo key). Run the extractor, then the completeness gate:

```
node <plugin>/resources/generate-links.js <workspace> --write
node <plugin>/resources/validate-links.js  <workspace>     # exit 1 = a source call has no edge
```

`generate-links.js` derives edges from actual code + config (verified signal chain):
- **REST:** each repo's `*.base-url` property → its `https://<host>` default → `<host>` first DNS label
  IS the callee repo key (caller = the declaring repo). Client `.uri()/.path()` literals are attached
  as `endpoints[]` and cross-checked against the callee's controller "provides" registry, so a specific
  path (e.g. `/containers`) is pinned — this is what the resolver's REST callee-expansion matches on.
- **Kafka/Temporal:** producer/consumer paired by topic/task-queue string (as before, 6a). These
  non-REST edges are preserved if already present.

`validate-links.js` re-derives and asserts every internal call has an edge:
- **ERROR (exit 1):** a base-url whose host resolves to a workspace repo but has no edge → fix before proceeding.
- **WARN:** an env-injected base-url with no default host that maps to no repo → a human classifies it
  internal (add a default host / glossary entry) or external. Silent misses are now impossible.

### STEP 7 — Build `_scenarios.json`, `_repo_router.json`, routing rules

**STEP 7a — `_scenarios.json`** (file-level phrase lookup):

```
sc = {}
FOR repo IN registered_repos:
  idx = read <workspace>/.asta-context/repos/<repo>/_index.json
  FOR entry IN idx.files:
    for phrase in entry.scenarios:
      sc[phrase].append({repo, path: entry.path})
write { generated_at: ISO_NOW, scenarios: sc } → <workspace>/.asta-context/_scenarios.json
```

**STEP 7b — `_repo_router.json`** (TOP-LEVEL agent router):

Execute `resources/prompts/03-repo-router.prompt.md` with the per-repo `_index.json`s, the draft `workspace.yml`, and `_global_links.json` as inputs. It emits one block:

```
=== REPO_ROUTER: _repo_router.json === … === END REPO_ROUTER ===
```

Write the body verbatim to `<workspace>/.asta-context/_repo_router.json`. Validation: if `request_buckets` is empty or `disambiguation_rules` is missing any collision token from `workspace.yml.routing_rules`, re-run the prompt with the gap called out.

**STEP 7c — `workspace.yml.routing_rules`** (broad-token fallback):

```
STOPWORDS = {service, model, controller, event, repository, dto, api}
per_repo_tokens = {}
FOR repo:
  tokens = lowercase(flatten(entry.primary_for for entry in <repo>/_index.json.files))
  tokens -= STOPWORDS
  per_repo_tokens[repo] = sort(unique(tokens))

routing_rules = []
seen = {}
for token in unique(flatten(per_repo_tokens.values)):
  owners = [repo for repo, toks in per_repo_tokens if token in toks]
  if len(owners) > 1:
    routing_rules.append({ match: [token], repos: owners })
    seen[token] = true

for repo, tokens in per_repo_tokens:
  unique_to_repo = [t for t in tokens if t not in seen]
  if unique_to_repo:
    routing_rules.append({ match: unique_to_repo, repos: [repo] })
```

### STEP 8 — Write `workspace.yml`, `lessons.md`, install drift hook

- `<workspace>/.asta-context/workspace.yml` per the schema in SKILL.md, populated from STEP 4 (repos + depends_on), STEP 7c (routing_rules), and STEP 6 (cross_repo_contracts copied verbatim from `_global_links.json` edges).
- Empty `<workspace>/.asta-context/lessons.md`.
- `mkdir -p <workspace>/.asta-context/hooks`
- Copy `resources/hooks/post-merge-context-drift.sh` to `<workspace>/.asta-context/hooks/post-merge-context-drift.sh` and `chmod +x` it.
- For each registered repo, write a one-line shim at `<repo>/.git/hooks/post-merge` that execs the workspace-level script with the repo key as the first argument. No prompt. (Repo `.git/hooks/` is local; never pollutes the repo itself.)

## Notes:

- Re-running this skill is safe. Repos with existing `<workspace>/.asta-context/repos/<key>/_index.json` are skipped.
- Sub-agents never see workspace state. Workspace concerns live only in `<workspace>/.asta-context/` and the outer loop in `orchestrate` / `solo.*`.
- Never write `CONSTITUTION.md`. Never emit `[INLINE]`/`[SPLIT]` markers. Never create `<repo>/.asta-context/`.
- Empty categories pruned.
- After scaffolding, every file's `verified_against` MUST equal `git rev-parse HEAD` for that repo at scaffold time.
- **Hard cap 150 lines per mini-skill.** Target 100–130. Split above 150.
- Every flow file MUST contain a "Variant Routing" table mapping trigger condition → resolver class (with source citation).
- The whole token-efficiency promise of this skill rests on `primary_for` vs `mentions` + the `_repo_router.json` top-level router. If the partitioner emits files with empty `primary_for`, refuse to write them. If `_repo_router.json` is empty or stale, the top-level routing collapses to brute-force token match.
