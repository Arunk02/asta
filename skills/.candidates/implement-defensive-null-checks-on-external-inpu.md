---
name: implement-defensive-null-checks-on-external-inpu
description: You are integrating with a service, API call, or method that might return null unexpectedly, leading to potential runtime NullPointerExceptions (NPEs).
tags: null, defensive, npe, validation
confidence: 0.95
source: extracted
---

# Implement defensive null checks on external inputs or return values

## When to use
You are integrating with a service, API call, or method that might return null unexpectedly, leading to potential runtime NullPointerExceptions (NPEs).

## Procedure
1. Identify the point in the code where a variable or function argument could potentially be null.
2. Implement an explicit conditional check (e.g., `if (value == null)`) immediately before using that variable.
3. Inside the 'null' branch of the logic, ensure that the execution path is graceful—either returning a default safe value, logging an error, or throwing a specific exception indicating invalid input.
4. If appropriate for the language/framework, consider using option types (e.g., Optional<T>) to enforce null safety at compile time.

## Pitfalls
- Only checking for null on some paths but not all possible execution flows, leading to a crash when an unhandled null is encountered.
- Over-checking: Adding checks everywhere even where the variable's origin guarantees non-null status (this adds unnecessary complexity).
- Assuming that `if (value)` is sufficient, as this only checks for falsiness, not strictly for null (e.g., an empty string or zero might evaluate to false but are technically valid inputs).

## Verification
- Write unit tests specifically passing null or invalid/empty values to the function/method under test.
- Confirm that the application handles these 'null' scenarios gracefully without crashing, and executes the intended fallback logic (e.g., returns a default object, logs an alert, or throws a specific domain exception).
