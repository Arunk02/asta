#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/**
 * validate-router.js — enforces coverage rules on _repo_router.json
 *
 * Usage:
 *   node validate-router.js <workspace-root>
 *
 * Expects (relative to workspace-root):
 *   .asta-context/workspace.yml          (read for routing_rules)
 *   .asta-context/_repo_router.json
 *   .asta-context/_global_index.json
 *   .asta-context/_global_links.json
 *
 * Exit codes:
 *   0  all checks passed
 *   1  validation failures (printed to stderr as JSON list)
 *   2  missing input files
 *
 * Zero deps — pure Node, runs anywhere with node >= 14.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
if (!ROOT) {
  console.error('usage: node validate-router.js <workspace-root>');
  process.exit(2);
}

const CTX = path.join(ROOT, CTX_DIR);
const ROUTER_PATH = path.join(CTX, '_repo_router.json');
const INDEX_PATH = path.join(CTX, '_global_index.json');
const LINKS_PATH = path.join(CTX, '_global_links.json');
const WORKSPACE_YML = path.join(CTX, 'workspace.yml');

function readJSON(p) {
  if (!fs.existsSync(p)) {
    console.error(JSON.stringify({ error: 'missing_file', path: p }));
    process.exit(2);
  }
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

function readWorkspaceYml(p) {
  // Line scanner for the routing_rules section. (A single `[\s\S]*?…\n*$` /m capture
  // stops at the first end-of-line and silently drops every rule but the first, which
  // made this collision check vacuous — see resolve-task.js for the same fix.)
  if (!fs.existsSync(p)) {
    console.error(JSON.stringify({ error: 'missing_file', path: p }));
    process.exit(2);
  }
  const text = fs.readFileSync(p, 'utf8');
  const listOf = (s) => s.split(',').map((t) => t.trim().replace(/^["']|["']$/g, '')).filter(Boolean);
  const rules = [];
  let section = null;
  let cur = null;
  for (const raw of text.split(/\r?\n/)) {
    if (!raw.trim() || raw.trim().startsWith('#')) continue;
    if (!/^\s/.test(raw)) { // column-0 → new top-level section
      const top = raw.match(/^([a-z_][\w]*):/);
      section = top ? top[1] : null;
      cur = null;
      continue;
    }
    if (section !== 'routing_rules') continue;
    let m = raw.match(/^\s*-\s*match:\s*\[([^\]]*)\]/);
    if (m) { cur = { match: listOf(m[1]), repos: [] }; rules.push(cur); continue; }
    if (cur) { m = raw.match(/^\s*repos:\s*\[([^\]]*)\]/); if (m) cur.repos = listOf(m[1]); }
  }
  return rules;
}

const router = readJSON(ROUTER_PATH);
const globalIndex = readJSON(INDEX_PATH);
const globalLinks = readJSON(LINKS_PATH);
const routingRules = readWorkspaceYml(WORKSPACE_YML);

const failures = [];
const warnings = [];

// ─ Rule 1: every primary_for token reachable from a bucket phrase ────────────
const allPrimaryFor = new Set();
const indexFiles = Array.isArray(globalIndex) ? globalIndex : globalIndex.files || [];
for (const entry of indexFiles) {
  for (const tok of entry.primary_for || []) allPrimaryFor.add(tok.toLowerCase());
}
const bucketPhrases = Object.keys(router.request_buckets || {}).map((p) => p.toLowerCase());
const unreachable = [];
for (const tok of allPrimaryFor) {
  const norm = tok.replace(/-/g, ' ');
  const hit = bucketPhrases.some((p) => p.includes(norm) || p.includes(tok));
  if (!hit) unreachable.push(tok);
}
if (unreachable.length) {
  failures.push({
    rule: 'primary_for_unreachable',
    message: 'primary_for tokens not reachable from any request_bucket phrase',
    tokens: unreachable.sort(),
  });
}

// ─ Rule 2: at least two synonym buckets per token ────────────────────────────
const tokenBucketCount = {};
for (const tok of allPrimaryFor) {
  const norm = tok.replace(/-/g, ' ');
  tokenBucketCount[tok] = bucketPhrases.filter((p) => p.includes(norm) || p.includes(tok)).length;
}
const singleBucket = Object.entries(tokenBucketCount)
  .filter(([, n]) => n === 1)
  .map(([t]) => t);
if (singleBucket.length) {
  warnings.push({
    rule: 'single_bucket_token',
    message: 'tokens with only ONE synonym bucket (consider adding a second phrasing)',
    tokens: singleBucket.sort(),
  });
}

// ─ Rule 3: every collision token has a disambiguation rule ───────────────────
const disambTokens = new Set((router.disambiguation_rules || []).map((r) => r.token));
const collisions = [];
for (const rule of routingRules) {
  if (rule.repos.length > 1) {
    for (const tok of rule.match) {
      if (!disambTokens.has(tok)) collisions.push({ token: tok, in_repos: rule.repos });
    }
  }
}
if (collisions.length) {
  failures.push({
    rule: 'collision_without_disambiguation',
    message: 'routing_rules collision tokens missing from disambiguation_rules',
    tokens: collisions,
  });
}

// ─ Rule 4: every flow has ≥ 5 unique lowercase triggers ──────────────────────
const allTriggers = new Map(); // trigger -> [flow names]
for (const flow of router.flows || []) {
  const trigs = flow.triggers || [];
  if (trigs.length < 5) {
    failures.push({
      rule: 'flow_trigger_count',
      message: 'flow has fewer than 5 triggers',
      flow: flow.name,
      count: trigs.length,
    });
  }
  for (const t of trigs) {
    if (t !== t.toLowerCase()) {
      failures.push({
        rule: 'flow_trigger_case',
        message: 'flow trigger is not lowercase',
        flow: flow.name,
        trigger: t,
      });
    }
    if (!allTriggers.has(t)) allTriggers.set(t, []);
    allTriggers.get(t).push(flow.name);
  }
}
for (const [trig, flows] of allTriggers) {
  if (flows.length > 1) {
    failures.push({
      rule: 'flow_trigger_overlap',
      message: 'trigger phrase appears in multiple flows (must be unique)',
      trigger: trig,
      flows,
    });
  }
}

// ─ Rule 5: every flow's repo_chain is a valid path in _global_links.json ─────
const linkEdges = new Map(); // producer -> Set(consumer)
const links = Array.isArray(globalLinks) ? globalLinks : globalLinks.edges || [];
for (const edge of links) {
  if (!linkEdges.has(edge.producer)) linkEdges.set(edge.producer, new Set());
  for (const c of edge.consumers || []) linkEdges.get(edge.producer).add(c);
}
for (const flow of router.flows || []) {
  const chain = flow.repo_chain || [];
  for (let i = 0; i + 1 < chain.length; i++) {
    const a = chain[i];
    const b = chain[i + 1];
    if (!linkEdges.get(a) || !linkEdges.get(a).has(b)) {
      failures.push({
        rule: 'flow_chain_invalid_edge',
        message: 'flow repo_chain edge not present in _global_links.json',
        flow: flow.name,
        from: a,
        to: b,
      });
    }
  }
}

// ─ Output ────────────────────────────────────────────────────────────────────
const report = {
  router_path: ROUTER_PATH,
  passed: failures.length === 0,
  failure_count: failures.length,
  warning_count: warnings.length,
  failures,
  warnings,
  stats: {
    primary_for_tokens: allPrimaryFor.size,
    request_buckets: bucketPhrases.length,
    flows: (router.flows || []).length,
    disambiguation_rules: disambTokens.size,
    global_link_edges: links.length,
  },
};

console.log(JSON.stringify(report, null, 2));
process.exit(failures.length === 0 ? 0 : 1);
