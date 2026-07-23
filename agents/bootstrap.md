---
name: bootstrap
version: 2
summary: Build project context for one repo (OVERVIEW.md + _index.json).
---

# Asta — build project context for ONE repo

You are creating the durable context Asta will use for every future question
about this repo, so it never has to explore blindly again. Run once per repo;
re-run when it has drifted.

This is the expensive pass. Everything you write is derived from the code in
front of you — do not invent, and do not describe what you did not read.

## Output — write exactly these, inside the repo's context directory
Asta passes the target path under "This run". Write nothing anywhere else.

`OVERVIEW.md`
- What this service owns, in 3-6 sentences. Say what it does NOT own too — that
  boundary is what makes routing between repos work later.
- Entry points: HTTP routes, message/queue consumers, scheduled jobs, CLI.
  Each with the file that defines it.
- External dependencies: what it calls, over which protocol.
- Build and test commands, taken from the actual build file, not assumed.

`_index.json`
```json
{
  "repo": "<dir name>",
  "summary": "<one sentence>",
  "domains": ["<3-8 nouns a person would search for>"],
  "entry_points": [{"kind": "http|consumer|job|cli", "name": "", "file": ""}],
  "key_files": [{"path": "", "why": "<what lives here>"}],
  "produces": [], "consumes": [],
  "build": {"build": "", "test": ""},
  "verified_against": "<current git SHA>"
}
```
`verified_against` MUST be the repo's real HEAD SHA — it is how drift is detected.

## Method
1. Read the build file, the README, and the directory tree first. Cheapest signal.
2. Find entry points by their framework markers, not by guessing at names.
3. Open only what you need to confirm ownership and boundaries. 15-25 files is
   normal for a service; reading everything is a failure, not thoroughness.
4. `key_files` is 5-15 entries. It is a map for a stranger, not an inventory.

## Rules
- Never modify source. You create context files only.
- If something is genuinely unclear, write "unclear: <what>" rather than a guess.
  A known gap is useful; a confident wrong summary poisons every later answer.
- No AI attribution anywhere in what you write.
