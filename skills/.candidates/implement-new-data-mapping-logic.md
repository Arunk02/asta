---
name: implement-new-data-mapping-logic
description: You have identified where data needs to be transformed or mapped from one structure (source) to another (target), and the implementation requires multiple code changes.
tags: mapping, data-transformation, integration
confidence: 0.65
source: extracted
---

# Implement new data mapping logic

## When to use
You have identified where data needs to be transformed or mapped from one structure (source) to another (target), and the implementation requires multiple code changes.

## Procedure
1. Review the source model definition and the target model definition to identify all fields that require mapping.
2. Implement the necessary transformation layer (e.g., using a dedicated mapper class, service method, or annotation processor).
3. Ensure proper error handling is added for cases where data conversion fails or null values are encountered.
4. Update associated consumer services and repository methods to utilize the new mapping logic.

## Pitfalls
- The source and target models have subtle naming differences (e.g., singular vs plural) that are overlooked, leading to runtime key errors.
- Missing edge cases during mapping (e.g., handling empty lists or optional fields) resulting in null pointer exceptions.
- Forgetting to update all consuming services after the mapper is implemented, causing components to use stale data.

## Verification
- Write unit tests that cover both ideal inputs and known failure/edge cases for the mapping logic.
- Run integration tests ensuring that services calling the mapped objects behave as expected with transformed data.
- Manually verify a sample request/response cycle to confirm data fidelity across the transformation boundary.
