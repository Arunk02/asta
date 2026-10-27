# Later reports reach the running investigation

#171 let a second person reporting the same failure join the investigation already running. They got their own reply, under their own permission. What they *said* never reached the worker, though: a different build, pod or timestamp in the second report was lost.

- **Saved as evidence:** a joining report is now queued as a colleague's evidence on that task (`issue_links.add_evidence`). It is only kept if it adds something the task has not seen; status pings ("any update?") and repeats are dropped. At most five reports are queued per task.
- **Never his words:** it is never put in `task_addenda`, which is Arun's channel and is relayed as "Arun said this himself".
- **One re-check:** when an analysis finishes, queued reports trigger one extra pass. The reports are wrapped as untrusted data, with the brief: "a different build, environment, booking or case ID is a different case — say so". If that pass fails, the first answer stands and says which reports were not checked.
- **Unchanged:** reply permission per reporter (#171/#174) is not affected.

Related, same batch: memory recall now scopes by workspace *before* the candidate limit, so a busy project can no longer fill every slot and hide the project that was asked about.

Limits: evidence that arrives during the re-check itself waits for the next investigation of that issue. Code tasks do not take colleague evidence mid-build. Matching is still #171's narrow service/environment correlation, not general semantic deduplication.
