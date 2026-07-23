#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/**
 * validate-indexes.js — integrity + symbol + bucket-sanity gate for the workspace indexes.
 *
 * Closes review gaps 2 (symbols unvalidated), 3 (no path-integrity gate), 4 (bucket correctness).
 *
 * Usage:
 *   node validate-indexes.js <workspace-root>
 *
 * Reads (relative to workspace-root/.asta-context):
 *   _global_index.json, _scenarios.json, _repo_router.json, _symbols.json, _global_links.json
 *   and the mini-skill / source files they reference on disk.
 *
 * HARD failures (exit 1):
 *   I1  every _global_index[*].path exists at repos/<repo>/<path>
 *   I2  every _scenarios[*][].path exists at repos/<repo>/<path>
 *   I3  every flows[*].entry_files[repo] exists at repos/<repo>/<path>
 *   S1  every _symbols.symbols[*][].path exists at repos/<repo>/<path>
 *   S3  every _symbols.aliases value is an owned token (some file's primary_for / domains)
 *
 * WARNINGS (exit 0, surfaced):
 *   S2  _symbols.symbols[*][].source exists at <repo>/<source>           (pin may drift)
 *   S4  class-like entities in _global_index absent from _symbols        (coverage gap)
 *   B1  request_bucket phrase routed to a repo with zero supporting file (suspect misroute)
 *
 * Exit: 0 pass (warnings allowed) · 1 hard failures · 2 missing input. Zero deps, node >= 14.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
if (!ROOT) { console.error('usage: node validate-indexes.js <workspace-root>'); process.exit(2); }
const CTX = path.join(ROOT, CTX_DIR);
const REPOS = path.join(CTX, 'repos');

function readJSON(p, optional) {
  if (!fs.existsSync(p)) {
    if (optional) return null;
    console.error(JSON.stringify({ error: 'missing_file', path: p }));
    process.exit(2);
  }
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

const globalIndexRaw = readJSON(path.join(CTX, '_global_index.json'));
const scenarios = readJSON(path.join(CTX, '_scenarios.json'));
const router = readJSON(path.join(CTX, '_repo_router.json'));
const symbols = readJSON(path.join(CTX, '_symbols.json'), true) || { symbols: {}, aliases: {} };

const indexFiles = Array.isArray(globalIndexRaw) ? globalIndexRaw : globalIndexRaw.files || [];

const failures = [];
const warnings = [];
const miniSkillExists = (repo, rel) => fs.existsSync(path.join(REPOS, repo, rel));
const sourceExists = (repo, src) => fs.existsSync(path.join(ROOT, repo, src));

// ── owned-token set (for S3 alias ownership) ────────────────────────────────
const ownedTokens = new Set();
const addTok = (t) => { if (t) { ownedTokens.add(String(t).toLowerCase()); ownedTokens.add(String(t).toLowerCase().replace(/-/g, ' ')); } };
for (const e of indexFiles) { (e.primary_for || []).forEach(addTok); (e.domains || []).forEach(addTok); }

// ── I1: _global_index paths exist ───────────────────────────────────────────
for (const e of indexFiles) {
  if (!e.repo || !e.path) { failures.push({ rule: 'index_entry_malformed', entry: e }); continue; }
  if (!miniSkillExists(e.repo, e.path)) failures.push({ rule: 'global_index_path_missing', repo: e.repo, path: e.path });
}

// ── I2: _scenarios paths exist ──────────────────────────────────────────────
for (const [phrase, locs] of Object.entries(scenarios)) {
  for (const loc of locs) {
    if (!loc.repo || !loc.path) { failures.push({ rule: 'scenario_loc_malformed', phrase, loc }); continue; }
    if (!miniSkillExists(loc.repo, loc.path)) failures.push({ rule: 'scenario_path_missing', phrase, repo: loc.repo, path: loc.path });
  }
}

// ── I3: flow entry_files exist ──────────────────────────────────────────────
for (const f of router.flows || []) {
  for (const [repo, rel] of Object.entries(f.entry_files || {})) {
    if (!miniSkillExists(repo, rel)) failures.push({ rule: 'flow_entry_file_missing', flow: f.name, repo, path: rel });
  }
}

// ── S1 / S2: symbol paths + sources ─────────────────────────────────────────
const symMap = symbols.symbols || {};
for (const [name, entries] of Object.entries(symMap)) {
  for (const e of entries) {
    if (!e.repo || !e.path) { failures.push({ rule: 'symbol_entry_malformed', symbol: name, entry: e }); continue; }
    if (!miniSkillExists(e.repo, e.path)) failures.push({ rule: 'symbol_path_missing', symbol: name, repo: e.repo, path: e.path });
    if (e.source && !sourceExists(e.repo, e.source)) warnings.push({ rule: 'symbol_source_missing', symbol: name, repo: e.repo, source: e.source });
  }
}

// ── S3: alias values owned ──────────────────────────────────────────────────
for (const [abbr, expansion] of Object.entries(symbols.aliases || {})) {
  const vals = Array.isArray(expansion) ? expansion : [expansion];
  for (const v of vals) {
    const norm = String(v).toLowerCase();
    if (!ownedTokens.has(norm) && !ownedTokens.has(norm.replace(/-/g, ' '))) {
      failures.push({ rule: 'alias_value_unowned', alias: abbr, value: v, message: 'alias expansion is not a primary_for/domains token of any file' });
    }
  }
}

// ── S4: class-like entities missing from symbols (coverage warning) ─────────
const isClassLike = (s) => /^[A-Z][a-z0-9]+[A-Za-z0-9]*$/.test(s) && /[a-z]/.test(s) && !/\s/.test(s);
const symKeys = new Set(Object.keys(symMap));
const missingClasses = new Set();
for (const e of indexFiles) for (const ent of e.entities || []) {
  if (isClassLike(ent) && !symKeys.has(ent)) missingClasses.add(ent);
}
if (missingClasses.size) {
  warnings.push({
    rule: 'symbol_coverage_gap',
    count: missingClasses.size,
    sample: [...missingClasses].sort().slice(0, 12),
    message: `${missingClasses.size} class-like entities in _global_index have no _symbols entry; identifier-lane tasks naming them fall back to NL routing`,
  });
}

// ── B1: bucket → repo with zero supporting file (Gap 4 correctness) ─────────
// Per repo, a "support corpus" = lowercased primary_for + mentions + scenarios + domains + title + entities.
const STOP = new Set(['service', 'model', 'controller', 'event', 'flow', 'data', 'api', 'the', 'and', 'for', 'with', 'not', 'to', 'in']);
const corpusByRepo = {};
for (const e of indexFiles) {
  const bag = corpusByRepo[e.repo] || (corpusByRepo[e.repo] = new Set());
  for (const arr of [e.primary_for, e.mentions, e.scenarios, e.domains, e.entities]) for (const t of arr || []) String(t).toLowerCase().replace(/-/g, ' ').split(/\s+/).forEach((w) => bag.add(w));
  String(e.title || '').toLowerCase().split(/\s+/).forEach((w) => bag.add(w));
}
let suspectBuckets = 0;
const suspectSample = [];
for (const [phrase, repos] of Object.entries(router.request_buckets || {})) {
  const words = phrase.toLowerCase().replace(/-/g, ' ').split(/\s+/).filter((w) => w.length >= 4 && !STOP.has(w));
  if (!words.length) continue;
  for (const repo of repos) {
    const bag = corpusByRepo[repo];
    if (!bag) { suspectBuckets++; if (suspectSample.length < 12) suspectSample.push({ phrase, repo, reason: 'repo has no indexed files' }); continue; }
    if (!words.some((w) => bag.has(w))) {
      suspectBuckets++;
      if (suspectSample.length < 12) suspectSample.push({ phrase, repo, reason: 'no phrase content-word in repo corpus' });
    }
  }
}
if (suspectBuckets) {
  warnings.push({
    rule: 'bucket_unsupported_route',
    count: suspectBuckets,
    sample: suspectSample,
    message: 'request_bucket phrases routed to a repo whose indexed files contain none of the phrase content words (possible misroute; cross-repo flow phrases are expected here)',
  });
}

// ── output ───────────────────────────────────────────────────────────────────
const report = {
  workspace_root: ROOT,
  passed: failures.length === 0,
  failure_count: failures.length,
  warning_count: warnings.length,
  failures,
  warnings,
  stats: {
    index_files: indexFiles.length,
    scenario_phrases: Object.keys(scenarios).length,
    symbols: Object.keys(symMap).length,
    aliases: Object.keys(symbols.aliases || {}).length,
    request_buckets: Object.keys(router.request_buckets || {}).length,
    flows: (router.flows || []).length,
  },
};
console.log(JSON.stringify(report, null, 2));
process.exit(failures.length === 0 ? 0 : 1);
