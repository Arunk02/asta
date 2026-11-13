---
name: propagate-field-logic-and-data-structures-across
description: A functional change or new piece of data originating in one core service needs to be mirrored, adapted, or consumed by a secondary, but functionally related, service/repository.
tags: cross-repository, data-migration, feature-parity
confidence: 0.75
source: extracted
---

# Propagate field logic and data structures across coupled repositories

## When to use
A functional change or new piece of data originating in one core service needs to be mirrored, adapted, or consumed by a secondary, but functionally related, service/repository.

## Procedure
1. Identify the source location of the required field/logic and the target repository that needs the update.
2. Implement the change in the primary (source) repository first, ensuring all local unit and integration tests pass.
3. Update the API contracts or data models in the target repository to accommodate the new field or logic.
4. Adapt any consumer code paths within the target repository, paying close attention to null handling or required transformations.
5. Execute end-to-end testing that validates the flow across the boundary between the two repositories.

## Pitfalls
- Assuming identical data types or naming conventions between repos (leading to runtime type errors).
- Forgetting to update shared dependency versions or interface definitions in the consumer repository.
- Testing only the successful path, thereby missing edge cases related to missing or malformed data from the source.

## Verification
- Successful compilation and test suite execution (including integration tests) in both the source and target repositories.
- Confirmation that upstream/downstream services consume the new field as expected without breaking existing functionality.
