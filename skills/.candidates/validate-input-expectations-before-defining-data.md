---
name: validate-input-expectations-before-defining-data
description: A mapping procedure fails or encounters unexpected runtime errors, suggesting that the underlying assumptions about source data state are incorrect or incomplete.
tags: mapping, data-validation, expectations, transformation
confidence: 0.85
source: teacher
---

# Validate input expectations before defining data mappings

## When to use
A mapping procedure fails or encounters unexpected runtime errors, suggesting that the underlying assumptions about source data state are incorrect or incomplete.

## Procedure
1. Review the existing mapping configuration and identify all fields requiring transformation.
2. Determine which external or internal states are assumed to be present (e.g., non-null values, required formats, specific relationship counts) but were not explicitly validated.
3. Implement explicit data validation checks immediately preceding the mapping execution logic, specifically targeting the 'fixed expectation' point.
4. Adjust the mapping procedure definition itself (configuration or code) to account for the detected edge case or unexpected state, rather than relying on default or implicit error handling.

## Pitfalls
- Relying solely on runtime failure detection without modifying the underlying assumption, leading to repeated escalations.
- Over-validating minor constraints while missing a fundamental, high-impact expectation regarding data structure or availability.
- Treating the symptom (the current failure) instead of fixing the root cause (the invalid expectation definition).

## Verification
- The mapping successfully executes in all defined test environments and passes the previously failing edge case scenario.
- A dedicated validation layer is visible and confirms expected data integrity before any transformation logic runs.
