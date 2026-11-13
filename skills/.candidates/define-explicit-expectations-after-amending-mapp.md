---
name: define-explicit-expectations-after-amending-mapp
description: When adding or modifying data mapping rules between two systems or structures where the resulting state is not immediately obvious from the code changes alone.
tags: mapping, schema, validation, dataflow, expectation
confidence: 0.90
source: teacher
---

# Define explicit expectations after amending mappings

## When to use
When adding or modifying data mapping rules between two systems or structures where the resulting state is not immediately obvious from the code changes alone.

## Procedure
1. Identify the source and target objects/schemas involved in the mapping amendment.
2. Determine the specific, non-obvious expected output state (e.g., a required field must be null; a calculation must round up; a relationship status must transition).
3. Implement a dedicated validation step or assertion immediately following the mapping application that explicitly verifies this calculated/expected state against the actual resulting data structure.
4. Ensure this new expectation check is included in the automated test suite run.

## Pitfalls
- The system appears to pass tests locally but fails in CI/CD because an implicit business rule or constraint (the 'expectation') was violated by the mapping change.
- Assuming that simply running unit tests covering input-output pairs is sufficient, thereby missing state transitions or data validation errors.

## Verification
- The entire task (adding and deploying the amended mapping) runs end-to-end successfully on the lower-tier automation flow.
- A new explicit assertion related to the expected post-mapping state is required, confirming that the system cannot complete the task without this step.
