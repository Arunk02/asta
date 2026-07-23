#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/*
 * validate-links.js — completeness gate for _global_links.json.
 *
 * Re-derives edges from source (generate-links logic) and asserts every INTERNAL cross-repo call is
 * represented. Converts the old silent-miss failure mode into a loud, file:line-pinned report:
 *   - ERROR: a base-url whose host resolves to a workspace repo but has NO edge (integrity break).
 *   - WARN:  an env-injected base-url with no default host that maps to no repo — a human must
 *            classify it internal (add a default host / glossary) or external.
 *
 * Usage: node validate-links.js <workspaceRoot>     exit 1 on ERROR, 0 otherwise (warnings printed).
 */
'use strict';
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
if (!ROOT) { console.error('usage: validate-links.js <workspaceRoot>'); process.exit(2); }
const CTX = path.join(ROOT, CTX_DIR);
const gen = path.join(__dirname, 'generate-links.js');

// derived truth (dry-run) vs what's on disk
let derived;
try { derived = JSON.parse(execFileSync('node', [gen, ROOT], { encoding: 'utf8' })); }
catch (e) { console.error('generate-links.js failed:', e.message); process.exit(2); }

let onDisk = [];
try { const d = JSON.parse(fs.readFileSync(path.join(CTX, '_global_links.json'), 'utf8')); onDisk = Array.isArray(d) ? d : d.edges || []; }
catch { console.error('_global_links.json missing — run generate-links.js --write'); process.exit(1); }

const diskRest = new Set(onDisk.filter((e) => e.protocol === 'rest').map((e) => e.producer + '>' + (e.consumers || [])[0]));
const errors = [];
for (const e of derived.rest_edges) {
  const k = e.producer + '>' + e.consumers[0];
  if (!diskRest.has(k)) errors.push(`MISSING EDGE: ${k} (derived from ${e.source}) not in _global_links.json`);
}
const warns = (derived.unresolved || []).map((u) => `UNRESOLVED: ${u.repo} ${u.property} @ ${u.file} — ${u.reason}`);

for (const w of warns) console.warn('WARN  ' + w);
for (const er of errors) console.error('ERROR ' + er);
console.log(`\nlinks: ${derived.rest_edges.length} internal REST edges expected, ${diskRest.size} on disk · ${errors.length} error(s), ${warns.length} warning(s)`);
process.exit(errors.length ? 1 : 0);
