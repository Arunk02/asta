---
name: validate-required-data-expectations-before-defin
description: When adding or significantly amending data mappings, and initial runs fail with cryptic contract mismatch errors that point to an assumption failure rather than a code bug.
tags: mapping, schema, validation, pre-flight
confidence: 0.90
source: teacher
---

# Validate required data expectations before defining mappings

## When to use
When adding or significantly amending data mappings, and initial runs fail with cryptic contract mismatch errors that point to an assumption failure rather than a code bug.

## Procedure
1. Identify all source and target schemas/data sources involved in the mapping task.
2. Run a dedicated schema validation check against these sources to confirm their structure matches the defined expectations (e.g., field presence, data type compatibility, mandatory constraints).
3. If any discrepancy is found during validation, halt the process immediately and log the specific missing or incorrect expectation that needs fixing.
4. Only proceed with the mapping definition/amendment steps after confirmation that all necessary pre-requisite expectations have been explicitly met and logged as validated.

## Pitfalls
- The task fails early in the pipeline with a generic 'expectation mismatch' or 'contract invalid' error, preventing access to the core logic failures.
- Validation succeeds but misses an implicit dependency (e.g., assuming data is non-null when it can sometimes be null) because the validation scope was too narrow.

## Verification
- The entire task runs to completion on the first attempt, and the execution logs clearly show a 'Schema Validation Passed' checkpoint before the main mapping logic begins.
- No escalation or manual adjustment is required after running this skill.
