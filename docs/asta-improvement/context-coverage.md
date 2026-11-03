# Entry points the context map does not know

Staleness (`context_gap`) counts commits since a repo's map was verified. It can't see a class that no mini-skill ever declared. The symbol index is generated from the mini-skills' `entities`, so such a class can't be found by exact name. A query for it falls back to general workspace context, and #204 labels that fallback a hint, not ownership.

On 31 October in the booking workspace, 21 of 29 Temporal activity classes were in no mini-skill, including the one a real task needed. Re-indexing can't fix that; enrichment has to add them.

`refresh.coverage_gap(workspace)` lists, per repo, the main-source entry points no mini-skill declares:

- Temporal activity and workflow implementations: any main-source class implementing an `@ActivityInterface` / `@WorkflowInterface` interface, wherever it lives, plus the `temporal/activity` path rule. A class counts as mapped if a mini-skill declares it or the interface it implements.
- `@KafkaListener` and `@RestController` classes, found by annotation.

It uses tracked files only, ignores tests, and is cached per repo HEAD and index file.

- **Code and debugging tasks:** their trust note names a repo with three or more unmapped entry points and gives examples. The worker greps the source instead of treating "not in the index" as "not in the repo".
- **A context refresh** (`enrich`, still on Arun's approval) is given the list of unmapped classes, asked to add each to the owning mini-skill with a source-backed line, and asked to regenerate the indexes. Its result reports "entry points mapped 14/36 → N/36". Before and after counts are kept in `context_provenance`.

Limits:

- Listeners and controllers are found by annotation, and Temporal classes by their interfaces. Other kinds of entry point aren't counted.
- This finds and asks for coverage; it doesn't write mini-skills itself.
- The booking refresh that would actually add them has not been run. It edits his workspace and uses model quota, so it waits for his go.
