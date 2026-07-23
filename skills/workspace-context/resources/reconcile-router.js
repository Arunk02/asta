#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/**
 * reconcile-router.js — additively wire any mini-skill tokens that are missing from the router
 * into `_repo_router.json.request_buckets`, so the resolver's bucket/scenario lanes can reach a
 * NEWLY ADDED mini-skill. Deterministic, additive-only: never removes or rewrites existing
 * buckets, flows, disambiguation_rules, or per_repo_summary (those stay LLM-curated).
 *
 * For every _global_index entry, each of its `primary_for` tokens and `scenarios` phrases that has
 * no request_buckets entry is added → [owning repo]. Existing entries are left untouched.
 *
 * Run after generate-indexes.js when a mini-skill was created/added:
 *   node reconcile-router.js <root> [--write]      (no --write → dry run, prints what it would add)
 *
 * Zero deps, node >= 14.
 */

'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
const WRITE = process.argv.includes('--write');
if (!ROOT) { console.error('usage: node reconcile-router.js <root> [--write]'); process.exit(2); }
const CM = path.join(ROOT, CTX_DIR);
const giPath = path.join(CM, '_global_index.json');
const rtPath = path.join(CM, '_repo_router.json');
for (const p of [giPath, rtPath]) if (!fs.existsSync(p)) { console.error(JSON.stringify({ error: 'missing_file', path: p })); process.exit(2); }

const index = JSON.parse(fs.readFileSync(giPath, 'utf8'));
const router = JSON.parse(fs.readFileSync(rtPath, 'utf8'));
router.request_buckets = router.request_buckets || {};

const norm = s => String(s).toLowerCase().trim();
const have = new Set(Object.keys(router.request_buckets).map(norm));
const added = [];

for (const e of index) {
  const phrases = [].concat(e.primary_for || [], e.scenarios || []);
  for (const ph of phrases) {
    const key = norm(ph);
    if (!key || have.has(key)) continue;
    router.request_buckets[ph] = [e.repo];
    have.add(key);
    added.push({ phrase: ph, repo: e.repo });
  }
}

console.log(JSON.stringify({ added: added.length, sample: added.slice(0, 8), write: WRITE }, null, 2));
if (WRITE && added.length) fs.writeFileSync(rtPath, JSON.stringify(router, null, 2) + '\n');
