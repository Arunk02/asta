<!-- GENERATED from SKILL.md "Step 5b" — do not edit by hand.
     Regenerate: python skills/workspace-context/build_enrich.py
     Why this file exists: re-enriching an existing repo needs the capture
     contract and nothing else. SKILL.md is ~6.9k tokens and most of it is
     bootstrap material for a NEW repo; this section is ~0.8k. Reading the whole
     skill on every drift pass is ~6k tokens of waste per run. -->

# Enrichment contract

### Step 5b — What counts as a valid capture (the enrichment quality bar)

The point of a mini-skill is to save an agent from rediscovering something. A line that does not do
that is not neutral — it is a permanent tax, loaded on every future task, pushing the genuinely useful
line further down the context window. **Adding nothing is better than adding noise.**

A fact earns its place only if an agent, arriving cold, would otherwise get the work WRONG without it.

| Capture | Skip |
|---|---|
| A contract: topic, endpoint, schema, queue name, event shape | "added a null check", "added logging", "renamed a variable" |
| An invariant or rule: *ATA wins over ETA*, *EXPORT skips customs* | A changelog line, or what a PR did |
| A decision with a reason: *retries capped at 3 because TMS 429s* | Restating what the code plainly says |
| Where a flow ENTERS and what it touches | Line-by-line narration of a method |
| A cross-repo edge: who produces, who consumes | A test that was added |
| A gotcha that has actually bitten: *this listener is not idempotent* | Anything already in a sibling mini-skill |

Four rules, and the last one is the one that gets broken:

1. **Every fact carries `(source: path:line)`.** No line, no fact — an unsourced claim cannot be
   verified later and is exactly what rots.
2. **Patch ≤10 lines.** A drifted mini-skill is corrected, not rewritten. Growth is the failure mode.
3. **Hard cap 150 lines per mini-skill.** At the cap, the fix is to SPLIT by concern or to delete
   something stale — never to keep appending.
4. **A patch that only restates the diff is a no-op — make it, and say nothing changed.** The commit
   that motivated the drift does not have to produce a context change. Most do not.

Stamp `verified_against = HEAD` even when nothing was written: the code WAS reviewed against the
context and found consistent, and leaving the sha behind means re-reviewing the same commits forever.

**A patch is not finished when the prose is written.** Three front-matter fields decide whether the
new fact can ever be found again, and `generate-indexes.js` does NOT back-fill them — it writes
`_global_index.json` from the per-repo `_index.json`, which is the WRITER's to maintain. Miss this
and the fact is real, correct, sourced, and unreachable:

| Field | What it buys | Miss it and… |
|---|---|---|
| `sources:` | the file is watched | the next change to it never marks this skill stale — it rots silently |
| `entities:` | the symbol lane | `resolve-task.js` answers `route: "ask"` for the exact class you documented |
| `scenarios:` | the natural-language lane | it is only findable by someone who already knows the symbol |

So: patch the `.md`, add the new source/entity/scenario to its front-matter, mirror those three
fields into `repos/<repo>/_index.json`, THEN run `generate-indexes.js` → `generate-symbols.js` →
`reconcile-router.js`. Verify with `resolve-task.js "<the new symbol>"` — a fact that does not route
was not captured, it was only typed.
