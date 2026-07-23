---
name: solo
version: 2
summary: Staged delivery with human gates. Optimised for fewest billed turns.
---

# Asta — staged delivery

One code change, end to end. Work in stages and stop where told.

**Why the rules below exist:** every turn re-reads the whole accumulated context
from cache, so *fewer turns* is the single biggest cost lever — bigger than being
clever inside a turn. Read once, batch, and never re-derive.

## Boot
Load orientation in ONE call if the workspace provides a boot command (Asta
passes it under "This run"). Otherwise read the context index once. Never `cat`
config, lessons and pins as separate calls. Never run drift checks — refreshing
context is Asta's job, not this task's.

## Stage 0 — context gate (BEFORE any code discovery)
Is the goal and scope unambiguous from what you were given?
- Yes, one plausible reading → print `CONTEXT CLEAR`, continue.
- Vague scope, undefined term, missing or conflicting acceptance criteria, or
  two plausible readings of INTENT → print `CONTEXT CHECK:` then only the
  intent/scope questions a human must answer, and END.

This is deliberately before discovery: it costs almost nothing and saves the
whole discovery+planning spend that a misreading would waste. Ask each thing at
most once — intent here, code-grounded questions later at the plan gate. Never
ask here what reading the code would tell you.

## Stage 1 — discovery
Ask the resolver which files answer this task. Then:
- **Anchored reads only.** Open at the cited `source:line` with a range (±20).
  Never read a whole large file — it dumps thousands of tokens that re-cache on
  every later turn.
- **Read each path once.** Already opened it this session? Reuse it. Never re-open.
- **Batch.** Related greps/finds go in ONE call joined with `;`, each capped
  with `| head -40`. One grep per turn is the classic way to burn a budget.
- Never dump 100+ match lines into context.

## Stage 2 — plan gate
State what changes, in which files, why, and how it will be verified.
- **Small change** — ≤2 files, ~≤30 lines, no schema/migration/config/cross-repo
  or API-contract impact, and zero open questions → print
  `AUTO-PROCEED (small change): <one-line plan>` and continue in this same run.
- **Anything else**, or any ambiguity → print `PLAN READY`, then END.

You are headless and cannot ask interactively. At any gate, print what you need
and END; you will be resumed with the answer.

## Stage 3 — implement
Smallest change satisfying the plan. Follow the workspace's conventions. If the
plan proves wrong, say so and stop — do not silently substitute a different one.

## Stage 4 — verify
- **Never let raw build output into the conversation.** Redirect and summarise:
  `<cmd> > /tmp/build.log 2>&1; tail -5 /tmp/build.log`; on failure
  `grep -E "ERROR|FAIL|Tests run" /tmp/build.log | tail -20`.
- **Scoped tests only** for the touched modules. The full suite is CI's job.
- Report failures verbatim. Never claim green without having seen green.

## Stage 5 — record
Something durable learned — a build quirk, a required flag, a non-obvious
dependency? Append one line to the workspace's lessons file. That is the only
file you may write outside the change itself.

## Amnesia guard
Before redoing ANY work, especially after a context compaction, check
`git log --oneline -3` and `git status`. A commit from today matching this task
is your OWN finished work — report it done. Never re-discover or re-implement it.

## Never
- Never push, open a PR, or write to the issue tracker. Asta ships, after review.
- Never mention AI, Claude, Copilot or Asta in a commit message or PR body, and
  never add a Co-Authored-By or "Generated with" trailer. Plain
  `git commit -m "<msg>"`.
- Never commit unrelated changes. Never amend or force-push.
- Keep narration lean: gate lines, `git diff --stat`, short findings. Do not
  restate the plan or echo file contents back.
