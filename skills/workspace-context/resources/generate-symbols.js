#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/**
 * generate-symbols.js — deterministically (re)build <workspace>/.asta-context/_symbols.json
 * from _global_index.json + a one-time source scan of each repo.
 *
 * Closes review gap 2: _symbols.json was generated out-of-band (the per-repo _index.json carries
 * no `sources`, so SKILL Step 7e's index-only build was impossible) and covered only ~54% of
 * class entities. This rebuilds it from the canonical inputs so it is complete and reproducible.
 *
 * Usage:
 *   node generate-symbols.js <workspace-root> [--write]
 *     (no --write → dry run, prints the diff vs the existing file and exits 0)
 *
 * Algorithm (deterministic):
 *   for each _global_index entry, for each class-like `entities[]` name E:
 *     find the repo source file whose basename stem == E (.java/.kt/.scala/.ts/.tsx/.py/.go/.cs)
 *     line = first `class|interface|enum|record|object E` declaration in that file
 *     symbols[E] += { repo, path: <mini-skill path>, source: <relative source>, line? }
 *   aliases = preserved from the existing _symbols.json (set by prompt 02 §5f).
 *
 * Zero deps, node >= 14.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
const WRITE = process.argv.includes('--write');
if (!ROOT) { console.error('usage: node generate-symbols.js <workspace-root> [--write]'); process.exit(2); }
const CTX = path.join(ROOT, CTX_DIR);

const SRC_EXT = new Set(['.java', '.kt', '.scala', '.ts', '.tsx', '.py', '.go', '.cs']);
const SKIP_DIR = new Set(['.git', 'node_modules', 'target', 'build', 'dist', '.gradle', '.idea', 'out']);

function readJSON(p) {
  if (!fs.existsSync(p)) { console.error(JSON.stringify({ error: 'missing_file', path: p })); process.exit(2); }
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

const gi = readJSON(path.join(CTX, '_global_index.json'));
const indexFiles = Array.isArray(gi) ? gi : gi.files || [];
const existing = fs.existsSync(path.join(CTX, '_symbols.json'))
  ? readJSON(path.join(CTX, '_symbols.json')) : { symbols: {}, aliases: {} };

const isClassLike = (s) => /^[A-Z][a-z0-9]+[A-Za-z0-9]*$/.test(s) && /[a-z]/.test(s) && !/\s/.test(s);

// ── one-time source scan per repo: stem → [relative paths] ───────────────────
const repoStemIndex = {}; // repo → Map(stem → [relpath])
function scanRepo(repo) {
  if (repoStemIndex[repo]) return repoStemIndex[repo];
  const map = new Map();
  const base = path.join(ROOT, repo);
  if (!fs.existsSync(base)) { repoStemIndex[repo] = map; return map; }
  const stack = [base];
  while (stack.length) {
    const dir = stack.pop();
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { continue; }
    for (const e of entries) {
      if (e.isDirectory()) { if (!SKIP_DIR.has(e.name)) stack.push(path.join(dir, e.name)); continue; }
      const ext = path.extname(e.name);
      if (!SRC_EXT.has(ext)) continue;
      const stem = path.basename(e.name, ext);
      const rel = path.relative(base, path.join(dir, e.name));
      if (!map.has(stem)) map.set(stem, []);
      map.get(stem).push(rel);
    }
  }
  repoStemIndex[repo] = map;
  return map;
}

function declLine(repo, rel, name) {
  try {
    const lines = fs.readFileSync(path.join(ROOT, repo, rel), 'utf8').split('\n');
    const re = new RegExp(`\\b(class|interface|enum|record|object|struct|type)\\s+${name}\\b`);
    for (let i = 0; i < lines.length; i++) if (re.test(lines[i])) return i + 1;
  } catch { /* ignore */ }
  return undefined;
}

// ── build ────────────────────────────────────────────────────────────────────
const symbols = {};
const addEntry = (name, entry) => {
  const arr = symbols[name] || (symbols[name] = []);
  if (!arr.some((x) => x.repo === entry.repo && x.path === entry.path)) arr.push(entry);
};

for (const e of indexFiles) {
  if (!e.repo || !e.path) continue;
  for (const ent of e.entities || []) {
    if (!isClassLike(ent)) continue;
    const stems = scanRepo(e.repo).get(ent) || [];
    if (stems.length) {
      // deterministic: shortest path first, then alphabetical
      const src = stems.slice().sort((a, b) => a.length - b.length || a.localeCompare(b))[0];
      const line = declLine(e.repo, src, ent);
      addEntry(ent, line ? { repo: e.repo, path: e.path, source: src, line } : { repo: e.repo, path: e.path, source: src });
    } else {
      addEntry(ent, { repo: e.repo, path: e.path }); // no source on disk → still a valid file route
    }
  }
}

// sort keys + entries for byte-stable output
const sortedSymbols = {};
for (const k of Object.keys(symbols).sort()) {
  sortedSymbols[k] = symbols[k].slice().sort((a, b) => a.repo.localeCompare(b.repo) || a.path.localeCompare(b.path));
}
const out = { symbols: sortedSymbols, aliases: existing.aliases || {} };

const before = Object.keys(existing.symbols || {}).length;
const after = Object.keys(sortedSymbols).length;
const withSource = Object.values(sortedSymbols).filter((a) => a.some((x) => x.line)).length;

if (WRITE) {
  fs.writeFileSync(path.join(CTX, '_symbols.json'), JSON.stringify(out, null, 2) + '\n');
}
console.log(JSON.stringify({
  written: WRITE,
  symbols_before: before,
  symbols_after: after,
  symbols_with_source_line: withSource,
  aliases: Object.keys(out.aliases).length,
}, null, 2));
