#!/usr/bin/env node
// Context directory name is supplied by the caller — Asta passes the name it
// detected for this workspace. Defaults to Asta's own layout.
const CTX_DIR = process.env.ASTA_CONTEXT_DIR || '.asta-context';

/*
 * generate-links.js — DETERMINISTIC cross-repo edge extraction from source.
 *
 * Replaces the LLM/name-match authoring of REST edges in _global_links.json (STEP 6b of
 * generate-workspace.md), which silently missed edges whenever a peer didn't string-match a repo key.
 *
 * Signal chain (compiler-grade, verified against IOM/booking):
 *   WebClientConfiguration binds a client bean to  services.<name>.base-url
 *   application*.yml resolves that property to      https://<host>...
 *   <host>'s first DNS label IS the repo key   →    caller (declaring repo) → callee (repo(host))
 *
 * Scope: REST edges are RE-DERIVED from source (this was the failure mode — LLM name-matching missed
 * them). A REST edge is emitted only when the caller repo actually has a REST client in src/main AND a
 * base-url whose host resolves to a workspace repo (a base-url host in a componenttest/mock config is
 * NOT a dependency). KAFKA / TEMPORAL / outbox edges are PRESERVED from the existing file — the setup
 * builds them by EXACT topic/task-queue STRING pairing (generate-workspace STEP 6a), which is reliable
 * (exact strings, unlike REST name-matching). The resolver's cross-repo expansion (§7a upstream, §7b
 * downstream) is protocol-agnostic and consumes REST/Kafka/Temporal edges uniformly; §7a2 adds the
 * REST-specific caller→callee promotion. validate-links.js gates REST completeness against source.
 *
 * Endpoint granularity: attaches client .uri()/.path() literals and cross-checks them against the
 * callee's controller "provides" registry so contractNamed() can match a specific path (e.g. /containers).
 *
 * Usage:  node generate-links.js <workspaceRoot> [--write]
 *   default: prints the derived edges + an unresolved report to stdout.
 *   --write: writes <workspaceRoot>/.asta-context/_global_links.json (rest re-derived, non-rest preserved).
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
const WRITE = process.argv.includes('--write');
if (!ROOT) { console.error('usage: generate-links.js <workspaceRoot> [--write]'); process.exit(2); }
const CTX = path.join(ROOT, CTX_DIR);

// ── repo keys + source roots (authoritative: workspace.yml) ───────────────────
function parseRepos() {
  const yml = fs.readFileSync(path.join(CTX, 'workspace.yml'), 'utf8');
  const repos = [];
  let cur = null;
  for (const line of yml.split('\n')) {
    const k = line.match(/^\s*-\s*key:\s*["']?([A-Za-z0-9._-]+)/);
    if (k) { cur = { key: k[1], root: k[1], domains: [] }; repos.push(cur); continue; }
    const r = line.match(/^\s*root:\s*["']?([^"'\n]+)/);
    if (r && cur) cur.root = r[1].trim().replace(/["']/g, '');
    const dm = line.match(/^\s*domains:\s*\[([^\]]*)\]/);
    if (dm && cur) cur.domains = dm[1].split(',').map((s) => s.trim()).filter(Boolean);
  }
  return repos;
}
const repos = parseRepos();
const keySet = new Set(repos.map((r) => r.key));
const repoDir = (r) => (r.root === '.' ? ROOT : path.join(ROOT, r.root));
// host → repo key: first DNS label must equal a repo key (else external to this workspace)
function repoForHost(host) { const label = host.split('.')[0]; return keySet.has(label) ? label : null; }
// token → repo via key-parts or domains (singularised): "charge"→charges domain→iom-master-data
const tokenIndex = new Map();
for (const r of repos) {
  const add = (w) => { const s = w.toLowerCase().replace(/s$/, ''); if (s.length >= 4 && !tokenIndex.has(s)) tokenIndex.set(s, r.key); };
  for (const d of r.domains || []) for (const w of d.split('-')) add(w);
  for (const w of r.key.split('-')) if (!/^(iom|example|service|svc)$/.test(w)) add(w);
}
function repoForToken(tok) { const s = String(tok).toLowerCase().replace(/s$/, ''); return tokenIndex.get(s) || null; }

// ── file walk (bounded: source + resources only, skip build/generated) ────────
function walk(dir, filter, out = []) {
  let ents; try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch { return out; }
  for (const e of ents) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      // skip build output AND component-test harness modules — a base-url host or client in a
      // componenttest config is a TEST mock, not a production cross-repo dependency (it created a
      // phantom email-service→booking-service edge). src/test (unit resources) is kept: real default
      // hosts often live there.
      if (/^(build|target|node_modules|\.git|\.gradle|generated|out|componenttest|component-test|integrationtest|integration-test)$/.test(e.name)) continue;
      walk(p, filter, out);
    } else if (filter(e.name)) out.push(p);
  }
  return out;
}
const isSrc = (n) => /\.(kt|java)$/.test(n);
const isYml = (n) => /application.*\.ya?ml$/.test(n);

// ── PROVIDES registry: controller endpoints per repo ──────────────────────────
// {repo: [{method, path}]}  path = class-level @RequestMapping prefix + method mapping
const provides = {};
const MAP_RE = /@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?(?:\[)?\s*"([^"]*)"/g;
for (const r of repos) {
  provides[r.key] = [];
  for (const f of walk(repoDir(r), isSrc)) {
    const txt = fs.readFileSync(f, 'utf8');
    if (!/@(Rest)?Controller|@RequestMapping/.test(txt)) continue;
    // class-level base path (first @RequestMapping before any @*Mapping method)
    const classBase = (txt.match(/@RequestMapping\s*\(\s*(?:value\s*=\s*)?(?:\[)?\s*"([^"]*)"/) || [])[1] || '';
    let m;
    const re = new RegExp(MAP_RE.source, 'g');
    while ((m = re.exec(txt))) {
      const method = m[1] === 'Request' ? 'ANY' : m[1].toUpperCase();
      let p = (classBase + m[2]).replace(/\/{2,}/g, '/');
      if (!p.startsWith('/')) p = '/' + p;
      provides[r.key].push({ method, path: p, file: path.relative(ROOT, f) });
    }
  }
}
// normalise a path to comparable segments (drop path-variable braces)
const segs = (p) => p.split('/').filter(Boolean).map((s) => s.replace(/\{[^}]*\}/g, '{}'));
function providerFor(clientPath) {
  const cs = segs(clientPath).filter((s) => s !== '{}');
  if (!cs.length) return [];
  const hits = [];
  for (const [repo, eps] of Object.entries(provides)) {
    for (const ep of eps) {
      const ps = segs(ep.path);
      // client path is a suffix of a provided path (client base-url already carries the prefix).
      // A provider path-variable ({}) may match a concrete client segment, BUT require ≥1 LITERAL
      // segment match — else a provider's trailing "/{id}" would match every 1-segment client path
      // (which wrongly attributed order-service paths to master-data).
      const tail = ps.slice(-cs.length);
      const shapeOk = tail.length === cs.length && tail.every((s, i) => s === cs[i] || s === '{}');
      const literalOk = tail.some((s, i) => s !== '{}' && s === cs[i]);
      if (shapeOk && literalOk) { hits.push({ repo, ep }); break; }
    }
  }
  return hits;
}

// ── CONSUMES: base-url properties (caller config) + client .uri()/.path() ─────
// property key -> {host?, envOnly, files[]}
const BASEURL_RE = /([A-Za-z0-9_.-]*base-?url)\s*:\s*(.+)/gi;
const HOST_RE = /https?:\/\/([a-z0-9-]+)\.[a-z0-9.-]+/i;

const restEdges = [];      // {producer, consumers:[callee], protocol, endpoints[], schema_path, source, notes}
const unresolved = [];     // internal-looking base-urls we couldn't map to a repo
// a repo emits REST edges only if it actually has a REST client in MAIN source — a base-url host in a
// componenttest/mock config is NOT a real dependency (email-service references a booking host in a test
// config but has zero WebClients → no edge).
function hasRestClient(r) {
  for (const f of walk(repoDir(r), isSrc)) {
    if (!/\/src\/main\//.test(f) && !/\/main\//.test(f)) continue;
    if (/WebClient|RestClient|RestTemplate|@FeignClient|FeignClient/.test(fs.readFileSync(f, 'utf8'))) return true;
  }
  return false;
}
for (const r of repos) {
  const dir = repoDir(r);
  const restCapable = hasRestClient(r);
  // 1) collect ALL internal hosts referenced anywhere in this repo's yaml (any profile carries the
  //    default host). Key by HOST, not by property name — nested yaml makes many services share the
  //    leaf key "base-url", which would otherwise collapse them and lose callees.
  const calleeHost = new Map();   // calleeRepoKey -> source file
  const envBaseUrls = [];         // base-url props with NO https host anywhere (candidate unresolved)
  for (const f of walk(dir, isYml)) {
    const txt = fs.readFileSync(f, 'utf8');
    const rel = path.relative(ROOT, f);
    let m; const hre = new RegExp(HOST_RE.source, 'gi');
    while ((m = hre.exec(txt))) {
      const callee = repoForHost(m[1]);
      if (callee && callee !== r.key && !calleeHost.has(callee)) calleeHost.set(callee, rel);
    }
    let b; const bre = new RegExp(BASEURL_RE.source, 'gi');
    while ((b = bre.exec(txt))) if (!/https?:\/\//.test(b[2])) envBaseUrls.push({ key: b[1].toLowerCase(), file: rel });
  }
  // 2) client .uri()/.path() literal paths in this repo (endpoint granularity)
  const clientPaths = [];
  for (const f of walk(dir, isSrc)) {
    const txt = fs.readFileSync(f, 'utf8');
    if (!/WebClient|RestClient|RestTemplate|@FeignClient|webClient/.test(txt)) continue;
    let m; const re = /\.(?:uri|path)\s*\(\s*"([^"]+)"/g;
    while ((m = re.exec(txt))) if (m[1].startsWith('/')) clientPaths.push(m[1]);
  }
  // 3) emit one edge per internal callee (host-resolved) — only if this repo really has a REST client
  if (restCapable) for (const [callee, file] of calleeHost) {
    const eps = [];
    for (const cp of [...new Set(clientPaths)]) if (providerFor(cp).some((h) => h.repo === callee)) eps.push(cp);
    restEdges.push({
      topic_or_endpoint: 'REST call',   // neutral (repos carried in producer/consumers; repo keys in the topic would false-match a task naming the repo)
      producer: r.key,
      consumers: [callee],
      protocol: 'rest',
      endpoints: eps,                    // resolver's restContractNamed matches a distinctive leaf here
      schema_path: `repos/${callee}/contracts/api-contracts.md`,
      payload: null,
      source: file,
      notes: `Deterministically derived from source: ${r.key} config references host → ${callee}. A caller change to a named request contract requires ${callee} (server) to honour it.`,
    });
  }
  // 4) env-injected base-urls with no host anywhere → resolve via domain/key token; else flag unresolved
  const seenUnres = new Set();
  if (restCapable) for (const { key, file } of envBaseUrls) {
    const token = key.replace(/-?base-?url$/, '').split(/[.\-_]/).filter(Boolean).pop() || '';
    if (!token || token.length < 3) continue;
    const callee = repoForToken(token);
    if (callee && callee !== r.key) {
      if (!calleeHost.has(callee)) { calleeHost.set(callee, file); restEdges.push({ topic_or_endpoint: 'REST call', producer: r.key, consumers: [callee], protocol: 'rest', endpoints: [], schema_path: `repos/${callee}/contracts/api-contracts.md`, payload: null, source: file, notes: `Token-derived (env-injected URL): ${key} → ${callee} via domain/key match.` }); }
    } else if (!callee && !seenUnres.has(key)) {
      seenUnres.add(key);
      unresolved.push({ repo: r.key, property: key, file, reason: 'env-injected URL, no default host and token matched no repo key/domain — classify internal (add default host / domain) or external' });
    }
  }
}

// dedup REST edges by (producer, callee)
const seen = new Set();
const restDedup = restEdges.filter((e) => { const k = e.producer + '>' + e.consumers[0]; if (seen.has(k)) return false; seen.add(k); return true; });

// ── ASYNC edges (KAFKA / TEMPORAL / outbox) — preserved from the existing file ─
// These are produced by the setup's EXACT topic / task-queue STRING pairing (STEP 6a of
// generate-workspace.md): a producer's topic/queue string is matched to consumers'. Exact-string
// matching is reliable (unlike the OLD REST name-matching), so async edges are carried through
// unchanged. Only REST needed source scanning (base-url host resolution) and is re-derived above.
// The resolver consumes all protocols uniformly (§7a/§7b are protocol-agnostic).
let existingArr = [];
try { const e = JSON.parse(fs.readFileSync(path.join(CTX, '_global_links.json'), 'utf8')); existingArr = Array.isArray(e) ? e : e.edges || []; } catch { /* none */ }
const preserved = existingArr.filter((e) => (e.protocol || '') !== 'rest');

const edges = [...restDedup, ...preserved];

if (WRITE) {
  fs.writeFileSync(path.join(CTX, '_global_links.json'), JSON.stringify(edges, null, 2));
  console.log(`WROTE ${edges.length} edges: ${restDedup.length} REST (source-derived) + ${preserved.length} async/other (kafka/temporal/outbox, preserved).`);
} else {
  console.log(JSON.stringify({ rest_edges: restDedup, preserved_non_rest: preserved.length, unresolved }, null, 2));
}
if (unresolved.length) console.error(`\n${unresolved.length} unresolved internal base-url(s) — see report (validate-links.js will fail on these).`);
