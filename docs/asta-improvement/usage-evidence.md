# Measure input tokens consistently

The scorecard omitted cache-write tokens from input totals and mixed executor-reported usage with character-count estimates. That could make an expensive first turn look cheaper or produce misleading trends when the source of the numbers changed.

The measured input average now includes fresh input, cache reads and cache writes. Estimated samples are shown separately and cannot receive a passing measured-efficiency score. Context statistics use measured samples. The displayed call count is explicitly labeled an approximation: dividing total input by maximum call context does not count actual calls.

Token metrics have a definition version. Older snapshots remain stored, but incompatible token values are omitted from the current trend rather than implying a workload change. Other historical metrics remain visible.

This corrects measurement; it does not itself reduce token use. Retained chat traces are not a complete task ledger, do not link to independently verified success, and are not a subscription bill. Token/cost per successful task and before/after savings still require a fixed representative workload, complete task usage, and verified outcomes.

Validation covers all three input categories, mixed measured/estimated samples, estimate-only data and versioned history. No private runtime trace data is committed.
