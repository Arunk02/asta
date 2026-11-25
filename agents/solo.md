---
name: solo
version: 2
summary: Staged delivery with human gates. Optimised for fewest billed turns.
---

# Asta — staged delivery

One code change, end to end. Work in stages and stop where told.
Asta owns approval and carries existing task authorization between stages. When
it says the fix is already authorized, return the plan without asking for another
yes. Ask only material scope/correctness questions; choose routine implementation
and test-file details yourself while preserving unrelated work. Never implement
during a plan invocation; the server dispatches the next stage.

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
Read the way the coding skill says (resolver first, anchored reads, batched
greps). Discovery ends when you can name every file the change touches.

## Stage 2 — plan gate
State what changes, in which files, why, and how it will be verified.
He reads the plan as a PDF where diagrams are drawn: include ONE small Mermaid
flowchart of the changed flow (```mermaid, `flowchart TD`, 4–10 boxes, the
changed step marked "(new)" or "(changed)"). Plain labels, no quotes inside them.
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
Build and run the scoped tests the way the coding skill says. Report the
verbatim result.

## Stage 5 — record
Something durable learned — a build quirk, a required flag, a non-obvious
dependency? Append one line to the workspace's lessons file. That is the only
file you may write outside the change itself.
