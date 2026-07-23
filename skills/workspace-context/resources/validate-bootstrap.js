#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/**
 * validate-bootstrap.js — enforces forensic-quality rules on the bootstrap output.
 *
 * Usage:
 *   node validate-bootstrap.js <workspace-root> [--repo <repo-key>]
 *
 * Scans <workspace>/.asta-context/repos/<repo>/{runtime,contracts,integrations,operations}/*.md
 * and applies the following hard rules:
 *
 *  R1. SOURCE ATTRIBUTION DENSITY
 *      Every file under runtime/, contracts/, integrations/, operations/ MUST contain
 *      at least 1 `(source: <…>)` attribution per 30 body lines (body = post front-matter).
 *      Files with < 1 attribution at all fail outright.
 *
 *  R2. SCENARIOS — USER VOCABULARY
 *      For every file (any category), front-matter `scenarios:` entries must be:
 *        a. ≤ 6 words (otherwise the phrase can't match a real task input)
 *        b. lowercase
 *        c. no CamelCase tokens (heuristic: any 2+-char run with adjacent UPPER+lower)
 *        d. ≥ 5 entries when `primary_for` is non-empty (per §5c discipline)
 *
 *  R3. PRIMARY_FOR vs SCENARIOS BUSINESS-NOUN BRIDGE
 *      For every `primary_for` token T, at least 2 `scenarios:` entries must contain the
 *      business noun in T (substring of T-with-hyphens-as-spaces, stopwords removed).
 *      Otherwise the scenario vocabulary is not aligned with the primary_for token,
 *      and _scenarios.json will never fire for tasks that mention T.
 *
 * Exit codes:
 *   0  all checks passed (warnings allowed)
 *   1  hard failures
 *   2  missing input (no bootstrapped repos found)
 *
 * Zero deps — pure Node, runs anywhere with node >= 14.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
if (!ROOT) {
  console.error('usage: node validate-bootstrap.js <workspace-root> [--repo <key>]');
  process.exit(2);
}
const REPO_FILTER = (() => {
  const i = process.argv.indexOf('--repo');
  return i > -1 ? process.argv[i + 1] : null;
})();

const REPOS_DIR = path.join(ROOT, CTX_DIR, 'repos');
if (!fs.existsSync(REPOS_DIR)) {
  console.error(JSON.stringify({ error: 'no_bootstrapped_repos', path: REPOS_DIR }));
  process.exit(2);
}

const FORENSIC_CATEGORIES = new Set(['runtime', 'contracts', 'integrations', 'operations']);
const STOPWORDS = new Set([
  'service', 'model', 'controller', 'event', 'repository', 'dto', 'api',
  'flow', 'data', 'config', 'request', 'response', 'error', 'consumer',
  'handler', 'message', 'processor', 'the', 'a', 'an', 'of', 'to', 'in',
  'is', 'are', 'was', 'were', 'be', 'and', 'or', 'not',
]);

const failures = [];
const warnings = [];
const stats = { files_scanned: 0, attributions_total: 0, scenarios_total: 0 };

/** Split a markdown file into { frontMatter, body } */
function splitFrontMatter(text) {
  if (!text.startsWith('---')) return { frontMatter: '', body: text };
  const end = text.indexOf('\n---', 3);
  if (end === -1) return { frontMatter: '', body: text };
  return { frontMatter: text.slice(4, end), body: text.slice(end + 4) };
}

/** Pull a YAML list from front matter (single-line `key: [a, b]` or block form). Best-effort regex. */
function getYamlList(fm, key) {
  // Inline: key: [a, b, c]
  let m = fm.match(new RegExp(`^${key}:\\s*\\[([^\\]]*)\\]`, 'm'));
  if (m) {
    return m[1].split(',').map((s) => s.trim().replace(/^["']|["']$/g, '')).filter(Boolean);
  }
  // Block:
  // key:
  //   - foo
  //   - bar
  const blockRe = new RegExp(`^${key}:\\s*\\n((?:\\s*-\\s.*\\n?)+)`, 'm');
  m = fm.match(blockRe);
  if (m) {
    return m[1].split('\n')
      .map((line) => line.replace(/^\s*-\s*/, '').replace(/^["']|["']$/g, '').trim())
      .filter(Boolean);
  }
  return [];
}

function hasCamelCase(str) {
  // Matches a 2+ run where uppercase is followed by lowercase or vice versa within a token
  return /[a-z][A-Z]|[A-Z]{2,}[a-z]/.test(str);
}

function businessNouns(token) {
  return token.toLowerCase()
    .replace(/-/g, ' ')
    .split(/\s+/)
    .filter((w) => w.length >= 4 && !STOPWORDS.has(w));
}

function checkFile(filePath, relPath, category, repoKey) {
  stats.files_scanned += 1;
  const text = fs.readFileSync(filePath, 'utf8');
  const { frontMatter, body } = splitFrontMatter(text);

  // ─ R1: source attribution density (forensic categories only) ───────────────
  if (FORENSIC_CATEGORIES.has(category)) {
    const bodyLines = body.split('\n').filter((l) => l.trim() && !l.trim().startsWith('#')).length;
    const attribCount = (body.match(/\(source:/g) || []).length;
    stats.attributions_total += attribCount;
    if (attribCount === 0 && bodyLines > 0) {
      failures.push({
        rule: 'source_attribution_missing',
        repo: repoKey,
        file: relPath,
        body_lines: bodyLines,
        attributions: 0,
        message: 'Forensic file has ZERO source attributions',
      });
    } else if (bodyLines >= 30 && attribCount / bodyLines < 1 / 30) {
      failures.push({
        rule: 'source_attribution_density_low',
        repo: repoKey,
        file: relPath,
        body_lines: bodyLines,
        attributions: attribCount,
        required: Math.ceil(bodyLines / 30),
        message: `Need ≥ 1 (source: …) per 30 body lines; got ${attribCount} for ${bodyLines} lines`,
      });
    }
  }

  // ─ R2: scenarios discipline ────────────────────────────────────────────────
  const scenarios = getYamlList(frontMatter, 'scenarios');
  const primaryFor = getYamlList(frontMatter, 'primary_for');
  stats.scenarios_total += scenarios.length;

  for (const s of scenarios) {
    const words = s.trim().split(/\s+/);
    if (words.length > 6) {
      failures.push({
        rule: 'scenario_too_long',
        repo: repoKey,
        file: relPath,
        scenario: s,
        words: words.length,
        message: 'scenario phrase exceeds 6 words; will never match real task input',
      });
    }
    if (s !== s.toLowerCase()) {
      failures.push({
        rule: 'scenario_not_lowercase',
        repo: repoKey,
        file: relPath,
        scenario: s,
        message: 'scenario phrase contains uppercase',
      });
    }
    if (hasCamelCase(s)) {
      failures.push({
        rule: 'scenario_camel_case',
        repo: repoKey,
        file: relPath,
        scenario: s,
        message: 'scenario contains CamelCase identifier — use user vocabulary instead',
      });
    }
  }

  if (primaryFor.length > 0 && scenarios.length < 5) {
    warnings.push({
      rule: 'scenarios_too_few',
      repo: repoKey,
      file: relPath,
      primary_for_count: primaryFor.length,
      scenarios_count: scenarios.length,
      message: 'File has primary_for tokens but < 5 scenarios entries; agent invisibility risk',
    });
  }

  // ─ R3: primary_for ↔ scenarios business-noun bridge ────────────────────────
  for (const tok of primaryFor) {
    const nouns = businessNouns(tok);
    if (nouns.length === 0) continue;
    const matches = scenarios.filter((s) => {
      const slc = s.toLowerCase();
      return nouns.some((n) => slc.includes(n));
    });
    if (matches.length < 2) {
      failures.push({
        rule: 'primary_for_scenario_bridge',
        repo: repoKey,
        file: relPath,
        primary_for_token: tok,
        business_nouns: nouns,
        scenarios_matching: matches,
        message: `primary_for "${tok}" has < 2 scenarios mentioning any business noun (${nouns.join(', ')}); _scenarios.json will never fire for this token`,
      });
    }
  }
}

function walkRepo(repoKey) {
  const repoDir = path.join(REPOS_DIR, repoKey);
  for (const category of ['domain', 'architecture', 'stack', 'runtime', 'contracts', 'integrations', 'operations', 'navigation']) {
    const dir = path.join(repoDir, category);
    if (!fs.existsSync(dir)) continue;
    for (const f of fs.readdirSync(dir)) {
      if (!f.endsWith('.md')) continue;
      const filePath = path.join(dir, f);
      checkFile(filePath, `${category}/${f}`, category, repoKey);
    }
  }
}

const repos = REPO_FILTER
  ? [REPO_FILTER]
  : fs.readdirSync(REPOS_DIR).filter((d) => fs.statSync(path.join(REPOS_DIR, d)).isDirectory());

for (const repoKey of repos) {
  if (!fs.existsSync(path.join(REPOS_DIR, repoKey, '_index.json'))) continue;
  walkRepo(repoKey);
}

const report = {
  workspace_root: ROOT,
  repos_scanned: repos,
  passed: failures.length === 0,
  failure_count: failures.length,
  warning_count: warnings.length,
  failures,
  warnings,
  stats,
};

console.log(JSON.stringify(report, null, 2));
process.exit(failures.length === 0 ? 0 : 1);
