#!/usr/bin/env node

/**
 * resolve-task.js — runs the Boot 0 §5–§7 repo/file routing algorithm ON DISK
 * and prints ONE compact result. The agent calls this instead of Reading the
 * five workspace index files, so the routing files never enter the LLM context.
 *
 * Usage:
 *   node resolve-task.js <workspace-root> "<task text>"
 *
 * Reads (from disk, NOT into the agent's context):
 *   .asta-context/workspace.yml         (repos, depends_on, routing_rules)
 *   .asta-context/_repo_router.json     (request_buckets, flows, disambiguation, per_repo_summary)
 *   .asta-context/_symbols.json         (identifier lane — only consulted when the task has an id/alias)
 *   .asta-context/_scenarios.json       (NL file lane)
 *   .asta-context/_global_index.json    (primary_for / mentions per mini-skill)
 *   .asta-context/_global_links.json    (cross-repo edges)
 *
 * Prints JSON:
 *   { route, impacted_repos, repo_order, matches[], entry_files, blast_radius[], candidates? }
 *
 * route ∈ symbol | flow | bucket | disambiguation | scenario | primary_for | mentions | nav | ask
 *
 * Deterministic. Zero deps. node >= 14.
 *
 * Exit: 0 resolved (route != ask) · 3 ask (agent must prompt user) · 2 missing input.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
const TASK = process.argv[3];
if (!ROOT || TASK == null) {
  console.error('usage: node resolve-task.js <workspace-root> "<task text>"');
  process.exit(2);
}
// Which directory holds the context — explicit name, else self-location (this
// file is copied INTO the context dir), else probe the known layouts. A
// hardcoded default would make `node .contmark/resolve-task.js .` fail on a
// workspace that is bootstrapped perfectly well.
function contextDirName(root) {
  const env = (process.env.ASTA_CONTEXT_DIR || '').trim();
  if (env) return env;
  if (fs.existsSync(path.join(__dirname, 'workspace.yml'))) return path.basename(__dirname);
  for (const cand of ['.contmark', '.asta-context']) {
    if (fs.existsSync(path.join(root, cand, 'workspace.yml'))) return cand;
  }
  return '.asta-context';
}
const CTX = path.join(ROOT, contextDirName(ROOT));

function readJSON(p) {
  if (!fs.existsSync(p)) { console.error(JSON.stringify({ error: 'missing_file', path: p })); process.exit(2); }
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

// ── §6b stopword list (parts ignored when split-matching) ────────────────────
const STOP = new Set([
  'service', 'model', 'controller', 'event', 'repository', 'dto', 'api',
  'flow', 'data', 'config', 'request', 'response', 'error', 'consumer',
  'handler', 'message', 'processor', 'the', 'a', 'an', 'of', 'to', 'in',
  'is', 'are', 'and', 'or', 'not', 'for', 'with', 'after', 'when', 'why',
]);

// ── minimal workspace.yml parse (repos / depends_on / routing_rules) ──────────
// Line scanner — NOT a single-regex section grab. A `[\s\S]*?…\n*$` capture with /m
// stops at the first end-of-line and silently drops every list item but the first
// (this is the latent bug in validate-router.js too). The scanner is robust to it.
function parseWorkspaceYml(p) {
  if (!fs.existsSync(p)) { console.error(JSON.stringify({ error: 'missing_file', path: p })); process.exit(2); }
  const text = fs.readFileSync(p, 'utf8');
  const listOf = (s) => s.split(',').map((t) => t.trim().replace(/^["']|["']$/g, '')).filter(Boolean);
  const repos = []; const rules = [];
  let section = null; let curRepo = null; let curRule = null;
  for (const raw of text.split(/\r?\n/)) {
    if (!raw.trim() || raw.trim().startsWith('#')) continue;
    if (!/^\s/.test(raw)) {                                   // column-0 → new top-level section
      const top = raw.match(/^([a-z_][\w]*):/);
      section = top ? top[1] : null; curRepo = null; curRule = null; continue;
    }
    if (section === 'repos') {
      let m = raw.match(/^\s*-\s*key:\s*(\S+)/);
      if (m) { curRepo = { key: m[1], domains: [], depends_on: [] }; repos.push(curRepo); continue; }
      if (curRepo) {
        m = raw.match(/^\s*domains:\s*\[([^\]]*)\]/); if (m) { curRepo.domains = listOf(m[1]); continue; }
        m = raw.match(/^\s*depends_on:\s*\[([^\]]*)\]/); if (m) { curRepo.depends_on = listOf(m[1]); continue; }
      }
    } else if (section === 'routing_rules') {
      let m = raw.match(/^\s*-\s*match:\s*\[([^\]]*)\]/);
      if (m) { curRule = { match: listOf(m[1]), repos: [] }; rules.push(curRule); continue; }
      if (curRule) { m = raw.match(/^\s*repos:\s*\[([^\]]*)\]/); if (m) { curRule.repos = listOf(m[1]); continue; } }
    }
  }
  return { repos, routing_rules: rules };
}

const ws = parseWorkspaceYml(path.join(CTX, 'workspace.yml'));
const router = readJSON(path.join(CTX, '_repo_router.json'));
const symbols = readJSON(path.join(CTX, '_symbols.json'));
const scenarios = readJSON(path.join(CTX, '_scenarios.json'));
const globalIndexRaw = readJSON(path.join(CTX, '_global_index.json'));
const links = (() => { const l = readJSON(path.join(CTX, '_global_links.json')); return Array.isArray(l) ? l : l.edges || []; })();

const indexFiles = Array.isArray(globalIndexRaw) ? globalIndexRaw : globalIndexRaw.files || [];
const repoKeys = ws.repos.map((r) => r.key);
const domainsByRepo = Object.fromEntries(ws.repos.map((r) => [r.key, r.domains]));

// ── §4b NORMALISE TASK ───────────────────────────────────────────────────────
let taskLc = TASK.toLowerCase();
const tokenise = (s) => s.toLowerCase().replace(/[^a-z0-9\s-]/g, ' ').split(/\s+/).filter(Boolean);
const taskTerms = new Set(tokenise(taskLc));
const ID_RE = /[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*/g;
const taskIds = Array.from(new Set(TASK.match(ID_RE) || []));
// DE-CAMEL BRIDGE: a CamelCase id ("ActivityPlanEvent") tokenises to one blob; split it
// into business words so bucket/scenario/routing matching on the noun ("activity plan") fires
// even when the class is not a known symbol.
for (const id of taskIds) {
  const words = id.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2').toLowerCase();
  for (const w of tokenise(words)) taskTerms.add(w);
  taskLc += ' ' + words;
}

// ALIAS EXPANSION — only the abbreviations actually typed expand
const aliases = symbols.aliases || {};
for (const [abbr, expansion] of Object.entries(aliases)) {
  if (taskTerms.has(abbr.toLowerCase())) {
    const exp = Array.isArray(expansion) ? expansion.join(' ') : String(expansion);
    for (const w of tokenise(exp)) taskTerms.add(w);
    taskLc += ' ' + exp.toLowerCase().replace(/-/g, ' ');
  }
}

// COMPOUND SPLIT — a single typed keyword for a multi-word concept ("serviceplan" / "service-plan")
// must match the spaced bucket phrase ("service plan"). Split hyphenated tokens always; split glued
// tokens only when BOTH halves are known router vocabulary (avoids bogus splits).
const vocab = new Set();
for (const phrase of Object.keys(router.request_buckets || {})) for (const w of tokenise(phrase)) if (w.length >= 3) vocab.add(w);
for (const term of Array.from(taskTerms)) {
  if (term.includes('-')) {
    for (const p of term.split('-')) if (p.length >= 3) { taskTerms.add(p); taskLc += ' ' + p; }
  } else if (term.length >= 8 && !vocab.has(term)) {
    for (let i = 3; i <= term.length - 3; i++) {
      const a = term.slice(0, i); const b = term.slice(i);
      if (vocab.has(a) && vocab.has(b)) { taskTerms.add(a); taskTerms.add(b); taskLc += ` ${a} ${b}`; break; }
    }
  }
}

// GLOSSARY GROUNDING — map ticket vocabulary to the codebase's canonical terms + owner repos.
// Fixes two failure modes at once: (a) a routing MISS when the ticket word is absent from every
// bucket (ticket says "flow" / "service type" but the code field is transportActivity), and
// (b) NAME DRIFT when the implementer invents a field from the ticket word instead of binding to
// the real symbol. Injects the canonical term (so symbol/bucket matching can fire on the real
// noun), seeds owner_repos as a recall net, and emits glossary_hits as the naming contract.
const glossaryHits = [];
const glossarySeed = new Set();
for (const g of router.glossary || []) {
  const alias = (g.aliases || []).find((a) => {
    const p = String(a).toLowerCase().replace(/-/g, ' ').trim();
    if (!p) return false;
    if (taskLc.includes(p)) return true;                 // phrase substring
    const words = p.split(/\s+/).filter((w) => w.length >= 3);
    return words.length > 0 && words.every((w) => taskTerms.has(w)); // all content words present
  });
  if (!alias) continue;
  glossaryHits.push({
    matched: alias, canonical: g.canonical, values: g.values || null, type: g.type || null,
    owner_repos: g.owner_repos || [], source: g.source || null, notes: g.notes || null,
  });
  for (const w of tokenise(String(g.canonical || '').replace(/([a-z0-9])([A-Z])/g, '$1 $2'))) taskTerms.add(w);
  taskLc += ' ' + String(g.canonical || '').toLowerCase();
  (g.owner_repos || []).forEach((r) => glossarySeed.add(r));
}

// ── matching primitives (substring OR content-word containment) ──────────────
const contentWords = (phrase) => tokenise(phrase).filter((w) => w.length >= 4 && !STOP.has(w));
// substring match only — for specific marker phrases where a single shared word must NOT count
function substringHit(phrase) {
  const p = phrase.toLowerCase().replace(/-/g, ' ').trim();
  return p.length > 0 && taskLc.includes(p);
}
function twoTier(phrase) {
  const p = phrase.toLowerCase().replace(/-/g, ' ').trim();
  if (!p) return false;
  if (taskLc.includes(p)) return true;            // substring
  const cw = contentWords(p);
  // content-word containment — require ≥2 shared content words; a single common word
  // ("send", "email") is too weak and produces false routes.
  return cw.length >= 2 && cw.every((w) => taskTerms.has(w));
}

// The org prefix every repo shares ("telikos-", "iom-", ""), DERIVED from the repo
// keys rather than hardcoded. A literal org name only works for one company and a
// placeholder like "example-" works for nobody: with it in place
// "telikos-booking-service" normalises to "telikosbookingservice", so a marker
// phrase of "booking" scores against the noise instead of the name. Derived, it is
// right for every workspace and needs no configuration.
const ORG_PREFIX = (() => {
  const keys = repoKeys.map((k) => k.toLowerCase());
  if (keys.length < 2) return '';
  const head = keys[0].split('-')[0];
  return head && keys.every((k) => k.startsWith(head + '-')) ? head + '-' : '';
})();

// prefix (e.g. "AP" / "booking" from *_marker_phrases) → repo key
function resolveRepoFromPrefix(prefix, candidates) {
  const pre = prefix.toLowerCase();
  const pool = candidates && candidates.length ? candidates : repoKeys;
  let best = null; let bestScore = 0;
  for (const key of pool) {
    let score = 0;
    const keyNorm = key.toLowerCase()
      .replace(ORG_PREFIX ? new RegExp('^' + ORG_PREFIX) : /^$/, '')
      .replace(/[-_]/g, '');
    if (keyNorm.includes(pre)) score = Math.max(score, 3);
    for (const d of domainsByRepo[key] || []) {
      const acr = d.split('-').map((w) => w[0]).join('');
      if (acr === pre) score = Math.max(score, 3);
      if (d.replace(/-/g, '').startsWith(pre)) score = Math.max(score, 2);
    }
    if (score > bestScore) { bestScore = score; best = key; }
  }
  return best;
}

// ═════════════════════════ REPO SELECTION (§5) ═══════════════════════════════
let route = null;
let impacted = [];
let entryFiles = {};
let symbolSeed = null;
let repoScores = null;
const trace = [];

// 5·0 SYMBOL HIT (highest precision)
const symMap = symbols.symbols || {};
for (const id of taskIds) {
  if (symMap[id]) {
    symbolSeed = symMap[id];
    impacted = Array.from(new Set(symbolSeed.map((e) => e.repo)));
    route = 'symbol';
    trace.push(`symbol hit: ${id} → ${impacted.join(',')}`);
    break;
  }
}

// 5a FLOW MATCH
if (!route) {
  for (const f of router.flows || []) {
    const hit = (f.triggers || []).find((t) => twoTier(t));
    if (hit) {
      impacted = (f.repo_chain || []).slice();
      entryFiles = f.entry_files || {};
      route = 'flow';
      trace.push(`flow match: "${hit}" → ${f.name}`);
      break;
    }
  }
}

// 5b REQUEST BUCKET MATCH (scored — prune weakly-matched repos when a clear leader exists)
if (!route) {
  const score = {};
  let matchedPhrase = null; let nPhrases = 0;
  for (const [phrase, repos] of Object.entries(router.request_buckets || {})) {
    if (twoTier(phrase)) { nPhrases++; matchedPhrase = matchedPhrase || phrase; repos.forEach((r) => { score[r] = (score[r] || 0) + 1; }); }
  }
  const reps = Object.keys(score);
  if (reps.length) {
    const max = Math.max(...reps.map((r) => score[r]));
    const thr = max >= 3 ? Math.ceil(max / 2) : 1; // prune single-graze repos only when some repo is strongly indicated (>=3 phrase hits)
    impacted = reps.filter((r) => score[r] >= thr);
    repoScores = score; route = 'bucket';
    trace.push(`bucket match: "${matchedPhrase}" (+${nPhrases - 1} more) scores=${JSON.stringify(score)} thr=${thr} → ${impacted.join(',')}`);
  }
}

// 5c DISAMBIGUATION (always runs when a collision token is present)
for (const rule of router.disambiguation_rules || []) {
  const tok = (rule.token || '').toLowerCase();
  if (!tok || !taskLc.includes(tok.replace(/-/g, ' ')) && !taskLc.includes(tok)) continue;
  // colliding repos for this token (from routing_rules) constrain prefix resolution
  const candidates = (ws.routing_rules.find((r) => r.match.includes(rule.token)) || {}).repos || [];
  let overridden = false;
  for (const k of Object.keys(rule)) {
    const m = k.match(/^(.*)_marker_phrases$/);
    if (!m) continue;
    const phrases = rule[k] || [];
    if (phrases.some((ph) => substringHit(ph))) {
      const owner = resolveRepoFromPrefix(m[1], candidates);
      if (owner) { impacted = [owner]; route = route || 'bucket'; route = 'disambiguation'; overridden = true; trace.push(`disambiguation: marker "${k}" → ${owner}`); }
      break;
    }
  }
  if (!overridden && rule.default_repo && impacted.length !== 1) {
    impacted = [rule.default_repo]; route = 'disambiguation';
    trace.push(`disambiguation: default_repo for "${tok}" → ${rule.default_repo}`);
  }
}

// 5d BROAD-TOKEN FALLBACK (routing_rules, scored) — only if still empty
if (!impacted.length) {
  const score = {};
  for (const rule of ws.routing_rules) {
    if (rule.match.some((t) => taskLc.includes(t.replace(/-/g, ' ')))) rule.repos.forEach((r) => { score[r] = (score[r] || 0) + 1; });
  }
  const reps = Object.keys(score);
  if (reps.length) {
    const max = Math.max(...reps.map((r) => score[r]));
    const thr = max >= 3 ? Math.ceil(max / 2) : 1;
    impacted = reps.filter((r) => score[r] >= thr);
    repoScores = score; route = 'broad_token';
    trace.push(`broad-token fallback scores=${JSON.stringify(score)} thr=${thr} → ${impacted.join(',')}`);
  }
}

// GLOSSARY RECALL NET — nothing matched a bucket/flow/symbol, but a known domain term did →
// route to that concept's owner repos (the parent/owner is never silently dropped).
if (!impacted.length && glossarySeed.size) {
  impacted = Array.from(glossarySeed);
  route = 'glossary';
  trace.push(`glossary seed → ${impacted.join(',')}`);
}

// empty → ask (still surface glossary_hits so the naming contract survives even with no route)
if (!impacted.length) {
  console.log(JSON.stringify({
    route: 'ask', impacted_repos: [], repo_order: [], matches: [], entry_files: {}, blast_radius: [],
    glossary_hits: glossaryHits, candidates: router.per_repo_summary || {}, trace,
  }, null, 2));
  process.exit(3);
}
impacted = Array.from(new Set(impacted));

// ═════════════════════════ FILE SELECTION (§6) ═══════════════════════════════
let matches = []; // {repo, path, source?, line?}
const inImpacted = (r) => impacted.includes(r);
const dedupe = (arr) => { const seen = new Set(); return arr.filter((m) => { const k = m.repo + '|' + m.path; if (seen.has(k)) return false; seen.add(k); return true; }); };

// 6·0 SYMBOL SEED
if (symbolSeed) {
  matches = symbolSeed.filter((e) => inImpacted(e.repo)).map((e) => ({ repo: e.repo, path: e.path, source: e.source, line: e.line }));
}

// 6a SCENARIO PHRASE HIT
if (!matches.length) {
  const hits = [];
  for (const [phrase, locs] of Object.entries(scenarios)) {
    if (twoTier(phrase)) for (const loc of locs) if (inImpacted(loc.repo)) hits.push({ repo: loc.repo, path: loc.path });
  }
  if (hits.length) { matches = dedupe(hits); if (route !== 'symbol') trace.push(`scenario hit → ${matches.length} file(s)`); }
}

// 6b primary_for / 6c mentions
function indexMatch(field) {
  const out = [];
  for (const e of indexFiles) {
    if (!inImpacted(e.repo)) continue;
    const toks = e[field] || [];
    const hit = toks.some((tok) => {
      const t = String(tok).toLowerCase();
      if (taskTerms.has(t)) return true;
      if (t.includes('-')) return t.split('-').some((p) => p.length >= 4 && !STOP.has(p) && taskTerms.has(p));
      return false;
    });
    if (hit) out.push({ repo: e.repo, path: e.path, category: e.category || e.path.split('/')[0] });
  }
  return out;
}
if (!matches.length) {
  let m = indexMatch('primary_for');
  let used = 'primary_for';
  if (!m.length) { m = indexMatch('mentions'); used = 'mentions'; }
  if (m.length) {
    // §6b/6c BREADTH CAP — per repo
    const byRepo = {};
    for (const f of m) (byRepo[f.repo] = byRepo[f.repo] || []).push(f);
    const capped = [];
    for (const [repo, files] of Object.entries(byRepo)) {
      if (files.length <= 6) { capped.push(...files); continue; }
      let narrowed = files.filter((f) => f.category === 'runtime' || f.category === 'contracts');
      if (narrowed.length > 6 || narrowed.length === 0) {
        narrowed = [
          { repo, path: 'navigation/scenarios.md', category: 'navigation' },
          { repo, path: 'navigation/key-classes.md', category: 'navigation' },
        ];
        trace.push(`breadth-cap ${repo}: ${files.length} files → navigation pair`);
      } else {
        trace.push(`breadth-cap ${repo}: ${files.length} → ${narrowed.length} (runtime+contracts)`);
      }
      capped.push(...narrowed);
    }
    matches = dedupe(capped.map((f) => ({ repo: f.repo, path: f.path })));
    route = route === 'flow' || route === 'symbol' ? route : route; // keep repo route
    if (used === 'mentions') trace.push('file route: mentions fallback');
    else trace.push('file route: primary_for');
  }
}

// 6d INDEX-MISS FALLBACK — navigation pair per repo
if (!matches.length && !Object.keys(entryFiles).length) {
  for (const r of impacted) {
    matches.push({ repo: r, path: 'navigation/scenarios.md' });
    matches.push({ repo: r, path: 'navigation/key-classes.md' });
  }
  matches = dedupe(matches);
  trace.push('index-miss → navigation fallback');
  route = 'nav';
}

// flow entry_files are the matches when a flow fired and nothing narrower hit
if (!matches.length && Object.keys(entryFiles).length) {
  matches = Object.entries(entryFiles).map(([repo, p]) => ({ repo, path: p }));
}

// ═════════════════ CROSS-REPO EXPANSION (§7) ═════════════════════════════════
// Gate: only expand when the link's CONTRACT is named in $resolve_text (topic + payload tokens),
// so a task doesn't drag in every producer of every topic an impacted repo merely touches.
// generic contract-scaffolding words that must not, on their own, "name" a contract
const CONTRACT_STOP = new Set(['type', 'types', 'base', 'url', 'rest', 'http', 'model', 'lookup', 'lookups',
  'request', 'response', 'service', 'services', 'cleanup', 'delete', 'patch', 'post', 'local', 'data', 'v1', 'v2', 'v3']);
function contractNamed(link) {
  const blob = `${link.topic_or_endpoint || ''} ${link.payload || ''}`;
  const toks = blob
    .replace(/([a-z])([A-Z])/g, '$1 $2')   // split camelCase: ServicePlanDomainEvent -> Service Plan Domain Event
    .toLowerCase().split(/[^a-z0-9]+/)
    .filter((t) => t.length >= 4 && !STOP.has(t) && !CONTRACT_STOP.has(t));
  // Require ≥2 DISTINCT contract tokens present in the task, OR one distinctive long identifier
  // (≥10 chars, e.g. a full CamelCase class). A single generic token ("container", "service")
  // is too weak — it dragged in unrelated contracts (master-data container-type lookups, offered-
  // service-plan cleanup) whenever the word merely appeared.
  const matched = [...new Set(toks.filter((t) => taskTerms.has(t) || taskLc.includes(t)))];
  return matched.length >= 2 || matched.some((t) => t.length >= 10);
}
// generic REST path segments that must NOT, on their own, name a contract
const GENERIC_SEG = new Set(['search', 'status', 'update', 'customer', 'download', 'incident', 'charges',
  'charge', 'multi', 'amend', 'finops', 'detail', 'details', 'create', 'delete', 'list', 'count', 'action',
  'behaviour', 'behavior', 'ref', 'numbers', 'associations', 'transportation']);
// endpoint-precise gate for REST callee-expansion: the task must name a DISTINCTIVE path segment of
// THIS edge (a specific resource like "containers"/"reprice"), not a generic one. Prefix/substring
// match handles singular↔plural ("container" ↔ "containers").
function restContractNamed(link) {
  if (contractNamed(link)) return true;
  const taskHas = (p) => { for (const t of taskTerms) if (t.length >= 5 && (p.includes(t) || t.includes(p))) return true; return false; };
  // Match on the LEAF resource segment of each endpoint. A single-part leaf ("containers") counts on
  // its own; a multi-part leaf ("container-and-cargo-weight-rules") needs ≥2 parts named — so a bare
  // "container" won't drag in master-data's container-type rules, but a real /containers call does.
  for (const ep of link.endpoints || []) {
    const parts = String(ep).split('/').filter(Boolean);
    let leaf = parts.pop() || '';
    if (/^\{.*\}$/.test(leaf)) leaf = parts.pop() || '';           // skip trailing path variable
    const words = leaf.replace(/\{[^}]*\}/g, '').replace(/[^a-z0-9-]/gi, '').toLowerCase().split('-')
      .filter((p) => p.length >= 5 && !GENERIC_SEG.has(p) && !CONTRACT_STOP.has(p));
    if (!words.length) continue;
    const hits = words.filter(taskHas).length;
    if (words.length === 1 ? hits === 1 : hits >= 2) return true;
  }
  return false;
}

// 7a UPSTREAM — pull producers of topics an impacted repo consumes, ONLY if the contract is named
for (const link of links) {
  if ((link.consumers || []).some((c) => inImpacted(c)) && !inImpacted(link.producer) && contractNamed(link)) {
    impacted.push(link.producer);
    trace.push(`upstream: +${link.producer} (produces ${link.topic_or_endpoint}; contract named)`);
  }
}
impacted = Array.from(new Set(impacted));

// 7a2 REST CALLEE — a caller repo that changes a REST request forces the SERVER (callee) to honour
// it. In the links' REST convention producer=caller, consumers=callee. Kafka §7b keeps the callee as
// advisory blast radius (a companion decision), but a REST request-contract change is not optional:
// promote the callee into repo_order so the parent/server repo is planned, not filed as a "risk".
for (const link of links) {
  if ((link.protocol || '') !== 'rest') continue;
  if (!inImpacted(link.producer) || !restContractNamed(link)) continue;
  for (const c of link.consumers || []) {
    if (!inImpacted(c)) {
      impacted.push(c);
      trace.push(`rest-callee: +${c} (server for ${link.topic_or_endpoint}; caller ${link.producer} impacted; endpoint named)`);
    }
  }
}
impacted = Array.from(new Set(impacted));

// 7b DOWNSTREAM blast radius — consumers of a contract an impacted repo produces, ONLY when that
// contract is NAMED in the task (symmetric with §7a). Un-gated, this surfaced every consumer of
// everything an impacted repo touches (master-data, facilities, scheduler) as noise. Gated, it
// surfaces only the downstream repos genuinely on THIS flow — which the agent's discovery gate then
// code-verifies and promotes into repo_order if impacted.
const blast = [];
for (const link of links) {
  if (inImpacted(link.producer) && contractNamed(link)) {
    for (const c of link.consumers || []) {
      if (!inImpacted(c) && !blast.find((b) => b.repo === c && b.topic === link.topic_or_endpoint)) {
        blast.push({ repo: c, contract: link.protocol === 'rest' ? 'contracts/api-contracts.md' : 'contracts/kafka-events.md', topic: link.topic_or_endpoint, schema_path: link.schema_path || null });
      }
    }
  }
}

// ═════════════════ TOPO SORT by depends_on (§8) ══════════════════════════════
function topoSort(nodes) {
  const set = new Set(nodes);
  const indeg = {}; const adj = {};
  nodes.forEach((n) => { indeg[n] = 0; adj[n] = []; });
  for (const r of ws.repos) {
    if (!set.has(r.key)) continue;
    for (const dep of r.depends_on) if (set.has(dep)) { adj[dep].push(r.key); indeg[r.key]++; }
  }
  const q = nodes.filter((n) => indeg[n] === 0).sort();
  const order = [];
  while (q.length) {
    const n = q.shift(); order.push(n);
    for (const m of adj[n].sort()) if (--indeg[m] === 0) q.push(m);
  }
  for (const n of nodes) if (!order.includes(n)) order.push(n); // safety (shouldn't happen; cycles broken)
  return order;
}
const repoOrder = topoSort(impacted);

// ── output ───────────────────────────────────────────────────────────────────
console.log(JSON.stringify({
  route,
  impacted_repos: impacted,
  repo_order: repoOrder,
  matches,
  entry_files: entryFiles,
  blast_radius: blast,
  scores: repoScores,
  glossary_hits: glossaryHits,
  trace,
}, null, 2));
process.exit(0);
