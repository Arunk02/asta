#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/**
 * generate-indexes.js — deterministically (re)build the two DERIVED index files
 * from the per-repo mini-skill indexes. No LLM needed.
 *
 *   <workspace>/.asta-context/_global_index.json  — every repo's _index.json files[], each prefixed
 *                                                with its "repo" key (flattened catalogue).
 *   <workspace>/.asta-context/_scenarios.json     — { "<scenario phrase>": [ {repo, path}, ... ] }
 *
 * Why this exists: generate-symbols.js builds _symbols.json and READS _global_index.json, while
 * validate-indexes.js REQUIRES _scenarios.json — but no script produced either. A from-scratch
 * setup therefore failed at validation. This closes that gap. Run order at setup:
 *
 *   node generate-indexes.js <root> --write   # 1. _global_index.json + _scenarios.json
 *   node generate-symbols.js <root> --write   # 2. _symbols.json (reads _global_index.json)
 *
 * Usage:
 *   node generate-indexes.js <workspace-root> [--write]
 *     (no --write → dry run: prints counts, writes nothing, exits 0)
 *
 * Zero deps, node >= 14.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
const WRITE = process.argv.includes('--write');
if (!ROOT) { console.error('usage: node generate-indexes.js <workspace-root> [--write]'); process.exit(2); }

const CTX = path.join(ROOT, CTX_DIR);
const REPOS = path.join(CTX, 'repos');
if (!fs.existsSync(REPOS)) { console.error(JSON.stringify({ error: 'missing_dir', path: REPOS })); process.exit(2); }

const repoKeys = fs.readdirSync(REPOS).filter(d => {
  try { return fs.statSync(path.join(REPOS, d)).isDirectory(); } catch (e) { return false; }
});

const global = [];
for (const key of repoKeys) {
  const idxPath = path.join(REPOS, key, '_index.json');
  if (!fs.existsSync(idxPath)) { console.error(JSON.stringify({ error: 'missing_index', repo: key, path: idxPath })); process.exit(1); }
  const idx = JSON.parse(fs.readFileSync(idxPath, 'utf8'));
  const repoName = idx.repo_key || key;
  for (const f of (idx.files || [])) global.push(Object.assign({ repo: repoName }, f));
}

const scenarios = {};
for (const e of global) {
  for (const s of (e.scenarios || [])) {
    (scenarios[s] = scenarios[s] || []).push({ repo: e.repo, path: e.path });
  }
}

const summary = {
  repos: repoKeys.length,
  global_index_entries: global.length,
  scenario_phrases: Object.keys(scenarios).length,
  write: WRITE,
};
console.log(JSON.stringify(summary, null, 2));

if (WRITE) {
  fs.writeFileSync(path.join(CTX, '_global_index.json'), JSON.stringify(global, null, 2) + '\n');
  fs.writeFileSync(path.join(CTX, '_scenarios.json'), JSON.stringify(scenarios, null, 2) + '\n');
}
