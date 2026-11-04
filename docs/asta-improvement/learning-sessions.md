# Learning sessions (`app/study.py`)

A learning session holds:

- a topic, and the objectives he set
- exercises, each written from a named source file in a registered workspace
- every answer he gave, and whether it held

It lives in the store, so it survives a restart. It never becomes an instruction: nothing here is fed to the skill catalog, memory or guardrails.

**Exercises.** An exercise carries the terms a right answer must contain. Every term must appear in its source when the exercise is written, or it is refused: a key the source does not support is a model's opinion, not the material. The source must sit inside the workspace root, under the same guard as `read_workspace_file`.

**Grading.** An answer is graded against the source as it reads at that moment.

- **passed:** the answer contains every term.
- **failed:** it doesn't; the result lists the missing terms.
- **unverifiable:** the source has gone, or no longer contains a term. This is never counted as passed or failed.
- A changed source that still supports the key is graded normally, with `source_changed` recorded.

An objective is met by one passed exercise. The session is complete when every objective is met.

**Tools** (group `study`):

- `study_start(topic, objectives)`
- `study_exercise(session_id, objective, question, answer_terms, workspace, source_path)`
- `study_answer(session_id, exercise_id, answer)`, which takes his words, unedited
- `study_status(session_id)`

Endpoints are under `/api/study`. The tools have their own group because a capability's whole group comes along with a top hit, and inside `tasks` they swelled unrelated turns past the tool cap.

**Limits.** Sources are workspace files only; a web page has to be saved into a workspace first. Grading checks that the required terms are present, not that the answer is understood. Writing good exercises is the brain's job, and the only thing enforced is that each one is grounded in its source.

This closes the benchmark cases `learn-session-persists` and `learn-answer-checked`.
