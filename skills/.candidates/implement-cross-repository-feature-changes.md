---
name: implement-cross-repository-feature-changes
description: When a functional change modifies a core business concept or data model that must be updated across multiple, independently managed repositories or microservices.
tags: cross-repo, multi-service, feature-implementation
confidence: 0.70
source: extracted
---

# Implement cross-repository feature changes

## When to use
When a functional change modifies a core business concept or data model that must be updated across multiple, independently managed repositories or microservices.

## Procedure
1. Identify all services/repositories affected by the domain change (e.g., 'transport priority').
2. Implement the necessary logic changes in each identified repository, prioritizing the most foundational service first.
3. Update data contracts, shared libraries, or APIs that govern the interaction between these repositories.
4. Write unit tests for the feature in every location it is used.
5. Execute end-to-end integration tests to confirm cross-service communication and functional consistency.

## Pitfalls
- Assuming a shared constant, enum, or data structure exists globally when it must be explicitly updated in each repo.
- Implementing the change in one repository without considering its downstream consumer dependencies (the 'leaf' service).
- Forgetting to run integration tests that span the boundaries of the modified services.

## Verification
- Unit tests pass across all involved repositories.
- Integration testing confirms the business flow executes correctly end-to-end, confirming the new priority logic works in context.
- Code has been merged/reviewed in all affected repos and branches are synchronized.
