---
name: propagate-feature-logic-across-coupled-repositor
description: A business logic or configuration change needs to be applied consistently across two or more distinct, but interdependent, codebases/repositories.
tags: cross-repo, integration, propagation, refactoring
confidence: 0.75
source: extracted
---

# Propagate feature logic across coupled repositories

## When to use
A business logic or configuration change needs to be applied consistently across two or more distinct, but interdependent, codebases/repositories.

## Procedure
1. Identify all repositories that must receive the modification (the scope).
2. Establish a common implementation pattern for the change. This ensures consistency regardless of the repository's internal structure.
3. Implement the change in the primary repository first, ensuring local unit tests pass and the feature works end-to-end.
4. Apply the exact same logic/configuration to each secondary repository (the 'propagation').
5. Execute integration tests across all affected services simultaneously. Pay close attention to how inter-repo communication handles the new data or priority.

## Pitfalls
- Implementing variations of the logic in different repos, leading to inconsistent behavior (e.g., Repo A uses an API call while Repo B uses direct database manipulation).
- Forgetting a minor service or consumer repo that relies on the data being propagated.
- Assuming shared library updates are sufficient without needing manual code changes in consuming services.

## Verification
- All necessary repositories confirm successful implementation of the new logic/configuration.
- The full suite of integration tests passes, validating the flow across all service boundaries.
- Manual smoke testing confirms that end-to-end user workflows utilizing the propagated feature are functional.
