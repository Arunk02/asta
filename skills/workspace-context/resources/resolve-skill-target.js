#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/**
 * resolve-skill-target.js — given a category + a fact's keywords, resolve WHICH mini-skill the
 * evolution-loop should patch in the workspace layout — or, if none owns the concept, the path of
 * the NEW slug to create. Makes the "predict the skill" step deterministic for the workspace
 * (`repos/<key>/<category>/<slug>.md`) layout, using the `primary_for`/`mentions`/`scenarios`
 * precision contract already in _global_index.json.
 *
 * Usage:
 *   node resolve-skill-target.js <root> --category <cat> --keywords "<words>" [--repo <key>]
 *     <cat> ∈ domain|architecture|stack|runtime|contracts|integrations|operations|navigation
 *   Output (JSON): { action: "patch"|"create", repo, path, score, candidates[] }
 *   exit 0 = resolved (patch or create) · exit 2 = bad args / need --repo to create
 *
 * Zero deps, node >= 14.
 */

'use strict';
const fs = require('fs');
const path = require('path');

function arg(name) { const i = process.argv.indexOf(name); return i > -1 ? process.argv[i + 1] : null; }
const ROOT = process.argv[2];
const CATEGORY = arg('--category');
const KEYWORDS = (arg('--keywords') || '').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
const REPO = arg('--repo');
if (!ROOT || !CATEGORY || !KEYWORDS.length) {
  console.error('usage: node resolve-skill-target.js <root> --category <cat> --keywords "<words>" [--repo <key>]');
  process.exit(2);
}
const CM = path.join(ROOT, CTX_DIR);
const gi = path.join(CM, '_global_index.json');
if (!fs.existsSync(gi)) { console.error(JSON.stringify({ error: 'missing_global_index', path: gi })); process.exit(2); }
const index = JSON.parse(fs.readFileSync(gi, 'utf8'));

const tokens = e => [].concat(e.primary_for || [], e.mentions || [], e.scenarios || [], [e.title || ''], e.domains || [])
  .join(' ').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);

const scored = index
  .filter(e => e.category === CATEGORY && (!REPO || e.repo === REPO))
  .map(e => {
    const t = new Set(tokens(e));
    let s = 0;
    for (const k of KEYWORDS) if (t.has(k)) s += 2;                                   // exact token hit
    for (const k of KEYWORDS) if (![...t].some(x => x === k) && [...t].some(x => x.includes(k) || k.includes(x))) s += 1; // partial
    // strong bonus: keyword appears in primary_for (ownership)
    const pf = (e.primary_for || []).join(' ').toLowerCase();
    for (const k of KEYWORDS) if (pf.includes(k)) s += 3;
    return { repo: e.repo, path: e.path, score: s };
  })
  .filter(x => x.score > 0)
  .sort((a, b) => b.score - a.score);

const out = { category: CATEGORY, keywords: KEYWORDS, candidates: scored.slice(0, 5) };

if (scored.length && scored[0].score >= 2) {
  out.action = 'patch';
  out.repo = scored[0].repo;
  out.path = scored[0].path;
  out.score = scored[0].score;
} else {
  // no clear owner → create a new slug
  const repo = REPO || (scored[0] && scored[0].repo);
  if (!repo) { console.log(JSON.stringify(Object.assign({ action: 'create', error: 'need --repo (no owning mini-skill to infer repo)' }, out), null, 2)); process.exit(2); }
  const slug = KEYWORDS.join('-').replace(/-+/g, '-');
  out.action = 'create';
  out.repo = repo;
  out.path = `${CATEGORY}/${slug}.md`;
  out.note = 'no mini-skill owns these keywords — create this slug, then re-run generate-indexes.js + generate-symbols.js';
}

console.log(JSON.stringify(out, null, 2));
process.exit(0);
