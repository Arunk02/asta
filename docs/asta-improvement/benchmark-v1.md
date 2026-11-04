# Benchmark v1

The fixed, versioned acceptance set from the roadmap's section 9 is `tests/workworld/benchmark/v1.yaml`:

- seven categories of ten cases each: coding, debugging, apps, communication, calls, memory, learning
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
- **Output:** results are written to `data/workworld/benchmark-v1.json`, with history in `benchmark-runs.jsonl`.

## First run, 4 November 2026 (deterministic, pass^3)

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
