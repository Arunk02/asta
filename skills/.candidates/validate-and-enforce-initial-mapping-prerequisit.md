---
name: validate-and-enforce-initial-mapping-prerequisit
description: Before executing any complex data transformation or object-to-object mapping where upstream dependencies are critical, particularly if previous runs failed due to unexpected input state.
tags: data-integrity, mapping, prerequisites, validation
confidence: 0.90
source: teacher
---

# Validate and enforce initial mapping prerequisites

## When to use
Before executing any complex data transformation or object-to-object mapping where upstream dependencies are critical, particularly if previous runs failed due to unexpected input state.

## Procedure
1. Identify all required external service states or datasets that must be present for the mapping process (the 'expectations').
2. Implement explicit pre-checks to confirm these dependencies meet minimum structural and data quality requirements.
3. If any prerequisite check fails, halt execution immediately and report a specific dependency failure error, rather than allowing the mapping logic to proceed with corrupted or missing assumptions.
4. Ensure that all necessary reference data mappings are loaded/initialized *before* the primary transformation loop begins.

## Pitfalls
- Relying on implicit state: Assuming that required data fields or services will be available when they are not, leading to runtime exceptions or silently incorrect output.
- Failure to test boundary conditions: The skill might pass when simple cases run, but fail spectacularly when edge case mappings (e.g., null values in expected non-null fields) are encountered.

## Verification
- The execution flow successfully passes the dependency checks without needing manual intervention or debugging of assumed state.
- The final mapped output structure conforms to the schema and contains the correct data types, confirming that the inputs were validated correctly at the start.
