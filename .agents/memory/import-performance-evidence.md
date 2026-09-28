---
name: Import performance evidence
description: Interpretation constraints for import throughput measurements
---

Use unprofiled, process-isolated concurrent imports for throughput acceptance; retain cProfile runs as diagnostic evidence, not production speed predictions.

**Why:** Local PostgreSQL wait time varied enough that a revision with fewer Python calls had a slower cProfile wall time. Earlier thread-based benchmark runs also understated process-worker throughput by reintroducing GIL contention.

**How to apply:** Compare the final implementation rather than selecting the fastest intermediate result. Report CPU quota, process isolation, committed row counts, workload scope, and unmet thresholds explicitly. The retained methodology and before/after evidence are in Audience Hub's docs/IMPORT_PERFORMANCE.md.

Keep large disposable database files on the workspace volume, not `/tmp`; keep only short Unix socket paths in `/tmp`.

**Why:** A real medium-seed verification exhausted the temporary filesystem quota despite `df` reporting ample free capacity. Regenerating the seed also wastes substantial setup time.

**How to apply:** Preserve generated CSVs separately from disposable database cleanup and reuse them after environment failures. Budget for real identity resolution as well as imports; preparing a medium dataset can take hours even when the operation being measured is short. Never substitute partially resolved profiles or dashboard-only timings for a full trait-job measurement.

Long benchmark infrastructure must outlive the shell tool's process tree, and resumed PostgreSQL clusters must use the same server major version.

**Why:** During resolver verification, shell-launched background processes were killed at the tool timeout. A detached launch inherited a different PostgreSQL version through PATH than the cluster creator.

**How to apply:** Use a persistent supervisor for multi-minute runs; verify server-version consistency and retained logs before interpreting a process termination as a workload failure. Always stop private servers explicitly after collecting results.