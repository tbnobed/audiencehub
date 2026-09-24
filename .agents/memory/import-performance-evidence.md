---
name: Import performance evidence
description: Interpretation constraints for import throughput measurements
---

Use unprofiled, process-isolated concurrent imports for throughput acceptance; retain cProfile runs as diagnostic evidence, not production speed predictions.

**Why:** Local PostgreSQL wait time varied enough that a revision with fewer Python calls had a slower cProfile wall time. Earlier thread-based benchmark runs also understated process-worker throughput by reintroducing GIL contention.

**How to apply:** Compare the final implementation rather than selecting the fastest intermediate result. Report CPU quota, process isolation, committed row counts, workload scope, and unmet thresholds explicitly. The retained methodology and before/after evidence are in Audience Hub's docs/IMPORT_PERFORMANCE.md.