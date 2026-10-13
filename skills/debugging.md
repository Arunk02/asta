---
name: debugging
description: >-
  Debug a case the way it was done for Vinish on 12 Oct — a booking, an order, a TO, an
  ack or an event that is stuck, missing, "not sent", "not consumed", or wrong, in ANY
  environment (prod, preprod, uat, sit, qa, dev, perf). Small queries built on each other
  with Asta's own tools: log_counts, log_lines, find_id, log_gaps, temporal_workflow(s).
  Also how logs and namespaces work here, and what a missing log line does and does not mean.
---

# Debugging a case

Asta's own tools build every query in code — the namespace, the two labels Loki demands,
the size cap, the rate limit. Do not write LogQL.

## Environments → where the logs are

| env | Loki namespace | Temporal namespace |
|---|---|---|
| prod | `telikos-prod` (with a hyphen) | `telikos-temporal-prod` |
| preprod / pp | `telikos-preprod` | `telikos-spt-cdt` |
| uat | `telikos-uat` | `telikos-sit-cdt` |
| sit | `telikos-sit` | `telikos-sit-cdt` |
| qa | `telikos-qa` | `telikos-e2e-test-cdt` |
| dev | `telikos-dev` | `telikos-temporal-cdt` |
| perf | `telikos-spt` | `telikos-perf-cdt` |

**Nobody named an environment? Count in all of them** (`log_counts env="all"`). Never
assume prod: Deepa's booking (12 Oct) was searched in preprod and sit, declared "not
sent to billing", and it lived — and was sent — in QA.

## The method — in this order

1. **Pin it.** The id(s), the environment, what is stuck or missing, what it should be.
2. **Where.** `log_counts` for the id by app, then by pod. Nothing anywhere → `find_id`:
   typed ids swap letters and digits ("H7QKN8NGNOR" was H7QKN8NGN9R; "T000…" was "TO00…").
3. **State and trail.** `log_lines` for the id, oldest first, or `grafana_logs` with
   errors_only=false for its whole trail. Logs are written in **UTC** — every tool prints
   UTC and local; never read one as the other (a search once looked 5½ hours off).
4. **The code.** Read the class that should have logged it or moved it on
   (`resolve_context`, `read_workspace_file`). Its exact log text is what to search for
   next; the transition that never ran names the event that never came.
5. **A missing line is a question, not an answer.** Before "not consumed / not sent /
   not received":
   - did the thing it starts run? `temporal_workflow history=true` shows each start and
     signal WITH its input — the ack for H7Y5BWNBNX9 was proved by its payload id;
   - did the downstream services log it?
   - `log_gaps` for that service and minute: per-pod lines against the log agent's
     dropped lines. 12 Oct: pod vm5rh was healthy (no restarts, 1% throttling) and quiet
     while its node dropped ~40k lines (`ingester_error`) — the consume lines were lost
     logs, not missed events.
6. **Answer.** Timeline · Why (mechanism, Class:line) · Not confirmed, and why · Next step.

## Rules that keep the answer true

- **Quote, don't paraphrase.** Give the exact line and the Grafana link `log_lines`
  prints — when someone asks for "the log link", that is it. A line saying
  `exceptionCode 400 … status PENDING` is not "billing returned 400".
- **None found is bounded.** It holds for that window, service and environment only.
  Prod logs are kept about 8 weeks; older than that, work from the state services
  report (API reads, DB, Temporal).
- **One answer per question.** If a later finding differs from an earlier one, say
  which was wrong and why — never two answers that disagree.
- **Prod config comes from Helm.** `<repo>/helm/prod-values.yml`; a key absent there
  means the `${VAR:default}` in application.yml applies — an answer, not an unknown.
- **The logs decide; the code explains.** A cause the logs do not show is a hypothesis —
  say so.
- **Don't re-run** a query whose answer you already have; build the next one on it.
