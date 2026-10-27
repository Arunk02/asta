# Keep PR actions attached to the correct repository

PR numbers repeat across repositories and owners. Task lookup previously matched only the number in some paths; PR notes then selected the first PR on a task. An approved note for a second repository could update the first repository instead.

Task continuation, staged notes and spoken walkthroughs now resolve one recorded PR using its full owner/repository identity, a known service name, or an unambiguous number. Unknown explicit targets never fall back to a number match. Ambiguous targets or duplicate task ownership require identifying the target instead of choosing the latest task.

The recorded operation carries the selected PR URL through the API and execution. Multi-PR notes require that target, which is checked against the task again before any GitHub operation. Description edits are reported complete only after the full appended text is read back successfully.

Tests use synthetic repositories with colliding numbers and fake GitHub commands. They exercise different owners, different repositories, multi-PR tasks, changed/unknown targets, body references that must not redirect the operation, ambiguous walkthroughs and failed/partial readback.
