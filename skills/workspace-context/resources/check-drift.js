#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/**
 * check-drift.js — on-demand context drift detection. Replaces the post-merge bash hook
 * AND the persistent _drift.json: computes staleness from data already on disk.
 *
 * For each repo it compares the mini-skills' `verified_against` SHA to the repo's current HEAD.
 * If they differ, it diffs the range and maps changed files to the EXACT stale mini-skills via
 * each mini-skill's front-matter `sources:` list (precise) — falling back to category
 * classification for changed files no mini-skill claims (coarse, flags a possible gap / new file).
 *
 * No hook to install, no ledger to maintain, no AI. Agents call this at Boot 0; evolution-loop
 * consumes its output as the code-change trigger.
 *
 * Usage:
 *   node check-drift.js <root> [--json]
 *     <root> = dir holding .asta-context (workspace parent, or repo root in single mode)
 *   exit 0 = no drift · exit 1 = drift found · exit 2 = bad args / not bootstrapped
 *
 * Zero deps, node >= 14.
 */

'use strict';
const fs = require('fs');
const path = require('path');
const cp = require('child_process');

const ROOT = process.argv[2];
const JSON_OUT = process.argv.includes('--json');
if (!ROOT) { console.error('usage: node check-drift.js <root> [--json]'); process.exit(2); }
const CM = path.join(ROOT, CTX_DIR);
const REPOS = path.join(CM, 'repos');
if (!fs.existsSync(REPOS)) { console.error(JSON.stringify({ error: 'not_bootstrapped', path: REPOS })); process.exit(2); }

function git(dir, args) {
  try { return cp.execFileSync('git', ['-C', dir].concat(args), { encoding: 'utf8' }).trim(); }
  catch (e) { return null; }
}

// path -> category (ported from the old post-merge hook; coarse fallback only)
function classify(f) {
  const cats = [];
  if (/(build\.gradle|pom\.xml|settings\.gradle|package\.json|pyproject\.toml|requirements.*\.txt|Cargo\.toml|go\.mod|libs\.versions\.toml|tsconfig.*\.json)$/.test(f)) cats.push('stack');
  if (/(nx\.json|workspace\.json|Makefile|\/bom\/|-bom|\/platform\/)/.test(f)) cats.push('architecture');
  if (/(controller|router|route|handler|endpoint|Application|Main.*\.(kt|go))/i.test(f)) cats.push('navigation');
  if (/(client|producer|publisher|outbound|gateway|external|consumer|listener)/i.test(f)) cats.push('integrations');
  if (/(\.proto|\.avsc|Dto|Request|Response|Event.*\.(kt|java)|Schema|Migration|\/V.*__.*\.sql)/.test(f)) cats.push('contracts');
  if (/(\.env|application.*\.(yml|yaml|properties)|feature-flag|launchdarkly|logback|logging|RetryConfig|TimeoutConfig|CircuitBreaker)/i.test(f)) cats.push('operations');
  if (/(Service.*\.(kt|java)|UseCase|Workflow.*\.kt|Activity.*\.kt)/.test(f)) cats.push('runtime');
  if (/(domain\/|Entity.*\.(kt|java)|Aggregate)/.test(f)) cats.push('domain');
  return cats;
}

// extract front-matter sources[] from a mini-skill .md (handles block list AND inline [a, b])
function readSources(md) {
  const txt = fs.readFileSync(md, 'utf8');
  const m = txt.match(/^---\n([\s\S]*?)\n---/);
  if (!m) return [];
  const fm = m[1];
  const out = [];
  const inline = fm.match(/^sources:\s*\[([\s\S]*?)\]/m);
  if (inline) {
    inline[1].split(',').forEach(s => { const v = s.trim().replace(/^['"]|['"]$/g, ''); if (v) out.push(v); });
    return out;
  }
  const lines = fm.split('\n');
  let i = lines.findIndex(l => /^sources:\s*$/.test(l));
  if (i === -1) return out;
  for (i++; i < lines.length; i++) {
    const l = lines[i];
    if (/^\s*-\s+/.test(l)) out.push(l.replace(/^\s*-\s+/, '').trim().replace(/^['"]|['"]$/g, ''));
    else if (/^\S/.test(l)) break; // next top-level key
  }
  return out;
}

const repoKeys = fs.readdirSync(REPOS).filter(d => { try { return fs.statSync(path.join(REPOS, d)).isDirectory(); } catch (e) { return false; } });
const report = { root: ROOT, drift: false, repos: [] };

for (const key of repoKeys) {
  const skillDir = path.join(REPOS, key);
  const idxPath = path.join(skillDir, '_index.json');
  if (!fs.existsSync(idxPath)) continue;
  const idx = JSON.parse(fs.readFileSync(idxPath, 'utf8'));
  const verified = idx.verified_against;
  // repo working tree: workspace = <root>/<key>; single = <root>
  let repoDir = path.join(ROOT, key);
  if (!fs.existsSync(path.join(repoDir, '.git'))) repoDir = ROOT;
  const head = git(repoDir, ['rev-parse', 'HEAD']);
  const entry = { repo: key, verified_against: verified, head, stale_mini_skills: [], unmatched_changed: [], impacted_categories: [] };

  if (!head) { entry.note = 'not_a_git_repo'; report.repos.push(entry); continue; }
  if (!verified || verified === head) { report.repos.push(entry); continue; }

  const diff = git(repoDir, ['diff', '--name-only', verified + '..' + head]);
  const changed = diff ? diff.split('\n').filter(Boolean) : [];
  if (!changed.length) { report.repos.push(entry); continue; }

  // precise: changed file in a mini-skill's sources[] => that mini-skill is stale
  const allMd = [];
  (function walk(d) { for (const e of fs.readdirSync(d)) { const p = path.join(d, e); const st = fs.statSync(p); if (st.isDirectory()) walk(p); else if (e.endsWith('.md')) allMd.push(p); } })(skillDir);
  const stale = new Set();
  const matchedFiles = new Set();
  for (const md of allMd) {
    const srcs = readSources(md);
    for (const c of changed) {
      if (srcs.some(s => c === s || c.endsWith('/' + s) || s.endsWith('/' + c) || c.endsWith(s))) {
        stale.add(path.relative(skillDir, md)); matchedFiles.add(c);
      }
    }
  }
  // coarse fallback: changed files no mini-skill claims => category flags (possible new code/gap)
  const cats = new Set();
  for (const c of changed) if (!matchedFiles.has(c)) classify(c).forEach(x => cats.add(x));

  entry.stale_mini_skills = [...stale].sort();
  entry.unmatched_changed = changed.filter(c => !matchedFiles.has(c));
  entry.impacted_categories = [...cats].sort();
  if (entry.stale_mini_skills.length || entry.impacted_categories.length) { entry.drift = true; report.drift = true; }
  report.repos.push(entry);
}

if (JSON_OUT) {
  console.log(JSON.stringify(report, null, 2));
} else {
  if (!report.drift) console.log('context up-to-date (no drift)');
  else for (const r of report.repos.filter(r => r.drift)) {
    console.log(`DRIFT ${r.repo}: ${r.verified_against ? r.verified_against.slice(0, 7) : '?'}..${r.head ? r.head.slice(0, 7) : '?'}`);
    if (r.stale_mini_skills.length) console.log('  stale mini-skills: ' + r.stale_mini_skills.join(', '));
    if (r.impacted_categories.length) console.log('  new/unclaimed code → categories: ' + r.impacted_categories.join(', ') + ` (${r.unmatched_changed.length} files)`);
  }
}
process.exit(report.drift ? 1 : 0);
