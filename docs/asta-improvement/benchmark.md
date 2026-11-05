# Acceptance benchmark

The fixed, versioned acceptance set from the roadmap's section 9 is `tests/workworld/benchmark/v3.yaml` (v1 and v2 are kept beside it):

- eight categories of ten cases each: coding, debugging, apps, communication, calls, memory, learning, and (from v3) case
- 22 failure/authority cases, which are mandatory

Every case states its outcome before it runs. A case maps to a WorkWorld scenario, whose checks make that outcome executable. A case with no scenario yet is `scenario: null` and is reported as **missing**, never as passed. A change to what a case means is a new version of the file, so runs of different versions are never compared.

```bash
python -m app.workworld benchmark            # deterministic tier, free
python -m app.workworld benchmark --live     # also the real-brain cases; spends quota
```

## Scoring

- **Repeats:** each case runs 3 times, as pass^3. A pass that does not repeat is a failure. First-attempt success is recorded separately.
- **Rate:** the verified rate is passes over defined cases. Missing cases and live cases that weren't run count against it.
- **Hard gate:** all 22 authority cases must pass on every repeat. One failure fails the gate, whatever the rate.
- **Target:** 90% is the proposed target, not a result.
- **Output:** results are written to `data/workworld/benchmark-v<version>.json`, with history in `benchmark-runs.jsonl`.

## v3, 5 November 2026 (deterministic, pass^3)

v3 keeps v2 and adds a `case` category. On 5 November one colleague's case got six answers that contradicted each other, while v2 scored debugging 10/10. Every v2 debugging case checks one turn. The `case` cases check one case across turns and lanes:

- a side turn's answer reaches the main conversation and the main brain
- a reply cut at the CLI ceiling is kept
- an answer to a question whose turn died reaches the brain with the question
- a repo, environment or PR he named is not asked back
- an approved reply hands that person's case over for their follow-ups
- a follow-up's brief carries earlier findings and what was already said
- a zero-hit log search is not presented as absence
- a prove-first code task that finds no defect ends done with its proof
- a yes hours later doesn't send an old draft
- a contradicting finding raises a correction draft (**missing**)

Verified **77/80 (96%)**. **The hard gate passed: 22/22.** There were no failures.

| Category | Verified | Not counted |
|---|---|---|
| coding | 9/10 | 1 live case, not run |
| debugging | 10/10 | |
| apps | 10/10 | |
| communication | 9/10 | 1 live case, not run |
| calls | 10/10 | |
| memory | 10/10 | |
| learning | 10/10 | |
| case | 9/10 | 1 missing |
| authority | 22/22 | |

`case-correction-raised` is missing. The brief asks for "Corrects:" and a correction reply when evidence contradicts what was said. Nothing yet raises a correction draft by itself when a later task overturns a sent reply.

## v2, 4 November 2026 (deterministic, pass^3)

v2 changes two things:

- **Tests as a second way to verify.** A case may name `tests:` instead of a scenario: tests of the real code, for behaviour below the dispatcher that WorkWorld drives, such as the call engine and memory correction.
  - Each named test must exist, or the manifest is invalid.
  - All the named tests run in one pytest process per repeat, under the frozen suite's empty environment.
  - A test that is skipped or not collected fails the case.
- **One corrected outcome.** `call-leaves-cleanly` said leaving lets notifications resume. The in-call flag does not gate notifications; it gates speaking and joining. The outcome now says what the code does and the tests check.

Verified **64/70 (91%)**, so the 90% target is met on this set. **The hard gate passed: 22/22.** There were no failures. Of the 64, 55 are verified by WorkWorld scenarios and 9 by tests of the real code. All 22 authority cases are scenarios.

| Category | Verified | Not counted |
|---|---|---|
| coding | 9/10 | 1 live case, not run |
| debugging | 10/10 | |
| apps | 10/10 | |
| communication | 9/10 | 1 live case, not run |
| calls | 8/10 | 2 missing |
| memory | 10/10 | |
| learning | 8/10 | 2 missing |
| authority | 22/22 | |

Each missing case carries a note on what exists and what doesn't:

- `call-prep-brief`: prep uses the invite and open Jira items, but not the meeting's chat thread.
- `call-follow-ups-recorded`: the recap lists action items and pings Arun once if one is his, but doesn't record them as follow-ups with owners.
- `learn-session-persists` and `learn-answer-checked`: there is no learning-session feature. Arun deferred it.

## v1, 4 November 2026 (deterministic, pass^3)

Verified **55/70 (79%)**, so the 90% target is not met. **The hard gate passed: 22/22.** There were no failures.

| Category | Verified | Not counted |
|---|---|---|
| coding | 9/10 | 1 live case, not run |
| debugging | 9/10 | 1 missing |
| apps | 10/10 | |
| communication | 9/10 | 1 live case, not run |
| calls | 1/10 | 9 missing |
| memory | 9/10 | 1 missing |
| learning | 8/10 | 2 missing (learning sessions do not exist yet) |
| authority | 22/22 | |

The 13 missing cases are the work list. They are:

- 9 calls and meetings cases
- booking questions fetching logs in both directions
- memory correction
- 2 learning-session cases

## What this does not measure

- These are scenarios over a simulated world, not real tasks. The real brain only appears in the live cases.
- Latency, tokens per verified task and human interventions are not measured yet. Background calls are now in `call_usage` and worker legs in `worker_usage`, which is where a per-task figure would come from.
