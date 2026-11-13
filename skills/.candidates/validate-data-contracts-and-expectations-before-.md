---
name: validate-data-contracts-and-expectations-before-
description: When adding any complex transformation or mapping layer between two services, if initial testing fails because the system behaves unexpectedly or requires manual adjustments to expected input/output types.
tags: data-mapping, schema-validation, contracts
confidence: 0.90
source: teacher
---

# Validate data contracts and expectations before implementing mappings

## When to use
When adding any complex transformation or mapping layer between two services, if initial testing fails because the system behaves unexpectedly or requires manual adjustments to expected input/output types.

## Procedure
1. Review existing service contracts (schemas) for both the source and target data structures involved in the mapping.
2. Identify all assumptions made about nullability, required fields, and acceptable value ranges for every mapped field.
3. Create dedicated contract validation tests that specifically test these boundary conditions and edge cases. These tests must fail if an expectation is violated.
4. Before writing transformation logic, ensure these validation tests pass against the current system state, explicitly confirming the expected data format (the 'contract') before proceeding with the mapping implementation.

## Pitfalls
- Relying solely on end-to-end integration tests, which can mask underlying contract mismatches by allowing faulty data to propagate until a major failure occurs.
- Assuming that existing code paths guarantee data format stability without explicitly running dedicated schema validation checks at the boundary.
- Treating 'fixing expectations' as an ad-hoc fix rather than identifying it as a required missing test or contract definition.

## Verification
- The mapping logic runs and passes all unit tests, specifically those targeting nullability, data type mismatches, and boundary value checks.
- The process of adding the mapping layer feels complete and requires no subsequent 'expectation fixes' to pass core tests.
