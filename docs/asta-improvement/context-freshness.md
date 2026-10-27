# A context refresh counts only when it is verified

`refresh.enrich` stamped a workspace "enriched" after every run, including runs that changed nothing or never moved a repo's `verified_against`. A workspace could therefore read as fresh while its map still described code that was over 100 commits old. It also hardcoded `.asta-context` as the context directory. In the booking workspace, which uses `.contmark`, the refresh's own edits were classed as stray files and left uncommitted, while the run still reported success.

Now:

- **Right directory.** The run edits and commits the context directory that workspace actually uses (`context_dirname`).
- **Verified freshness.** Each repo's `verified_against` and commits-behind are read before and after the run. The workspace is stamped fresh only when the run changed files in its context directory and, if any repo was stale (25+ commits behind or never verified), at least one stale repo's `verified_against` moved. Otherwise the report says "NOT brought up to date", names the repos still stale, and the stamp is not moved.
- **Provenance.** `context_provenance:<workspace>` records the time, whether the run counted as fresh, the context directory, the files changed, the commit result, and each repo's before/after `verified_against` and distance behind. Successful repos are reported as "svc: verified aaaa → bbbb (105 behind → 0 behind)".

Limits: freshness is per repo, measured against `origin/develop`. A moved `verified_against` shows the map was stamped against newer code, not that every mini-skill is correct; that is still the enrichment contract's job. Repos that were not stale and were not re-stamped are reported, not failed.
