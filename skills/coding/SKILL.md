---
name: coding
description: How Asta's code legs read, change, verify and commit code in his workspaces — anchored reads, Edit-only changes, scoped tests, the reviewer checklist, branch and commit rules. Load before changing code in any of his repos.
---

# Coding — how a change is made here

One source of truth for every code leg, whatever the pipeline (micro or staged)
and whichever brain runs it. The pipeline says WHEN to stop; this says HOW to work.
The rules that cost the most are also enforced by a hook on Claude legs: a
refused call is the rule, not a fault — follow the message and carry on.

## Read
- Ask the workspace resolver which files answer the task before any search.
- **Anchored reads only**: open at the cited `path:line` with an offset and a
  limit (about 80 lines). A whole large file stays in context and is re-read on
  every later turn. Read each range once; reuse what you already read.
- **Batch**: related greps go in ONE call joined with `;`, each capped with
  `| head -40`. Never dump hundreds of match lines.

## Change
- The smallest change that satisfies the plan. Follow the repo's conventions.
- Change files with **Edit** (Write only for a new file). Never through the
  shell — no `sed -i`, no redirect into a repo file, no script that writes one.
- If the plan proves wrong, say so and stop; never substitute a different plan.

## Before you commit — what a reviewer must never be the one to find
- Every value from a request, a payload, the database or another service can be
  missing. Guard every lookup on it (some collection types throw on a missing key
  or a null lookup), and add a test that passes the missing value.
- Never remove or rename a public method, a field in a message or schema, an enum
  value, a topic or a stored field without keeping the old one working: callers
  you cannot see still use it. A change to a durable workflow needs versioning so
  running instances still replay.
- Say in one line what behaviour changes and for whom, and keep a test proving
  the behaviour you did NOT mean to change is unchanged.
- Build, and run the tests you touched, before committing. Zero tests run is a
  failure, not a pass.

## Verify
- Never let raw build output into the conversation: `<cmd> > /tmp/build.log 2>&1;
  tail -5 /tmp/build.log`; on failure `grep -E "ERROR|FAIL|Tests run" /tmp/build.log | tail -20`.
- Scoped tests only, for what you touched. The full suite is CI's job.
- Report failures verbatim. Never claim green without having seen green.

## Branch
Asta has already cut your branch and checked it out in every repo of your own
worktree — the "THIS RUN" block names it. Commit there. Never `git checkout -b`,
never switch branch, and never report yourself blocked for want of a branch: you
have one. If the change reaches a repo you have no checkout for, SAY SO by name
and stop — Asta prepares it and continues this same task. Do not start a new one.

The one exception is an EXISTING branch or PR. When he says the work belongs on a
branch or pull request that already exists ("pick that PR", "fix it on the existing
branch", "update the same PR"), that is an instruction, not a question for him: find
it (`gh pr list --search`, `git branch -r`), `git fetch`, `git switch` this worktree
to it, commit there, and name the branch in your summary.

## Amnesia guard
Before redoing ANY work, especially after a compaction or in a fresh session
that starts from a HANDOFF, check `git log --oneline -3` and `git status`. A
commit from today matching this task is your OWN finished work — build on it.

## Never
- Never push, open a pull request, or write to the issue tracker. Asta ships, after review.
- Never mention AI, Claude, Copilot or Asta in a commit message, a branch name or
  a PR body, and never add a Co-Authored-By or "Generated with" trailer. Plain
  `git commit -m "<msg>"`.
- Never commit unrelated changes. Never amend or force-push.
- Keep narration lean: gate lines, `git diff --stat`, short findings.

## Reference — read only when the change touches it
- A durable workflow (workflow class, signal, activity order): `{SKILL_DIR}/references/workflows.md`
- A message schema, a serialized enum, a topic: `{SKILL_DIR}/references/messages.md`
- Building and testing in his services: `{SKILL_DIR}/references/build.md`
- A playbook learned from his past tasks may match: `load_skill(name)` when its
  one-line description fits the change.
