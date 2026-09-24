---
name: Import recovery intent
description: Why partially committed imports are intentional rather than an atomicity defect
---

Preserve committed import batches on later failure; do not restore whole-file
rollback as a cleanup or simplification.

**Why:** Production late failures discarded more than eight minutes of import
work. The user explicitly chose idempotent batch commits and resumable retries
over whole-file atomicity. Partial imported data is an intentional consequence.

**How to apply:** Changes to validation, identity coordination, error reporting
or reset behavior must preserve checkpoint/counter consistency and never silently
restart a partially committed import with a different mapping.

Keep identity lock cardinality and transaction lifetimes bounded; increasing
PostgreSQL's lock-table setting is only additional headroom.

**Why:** The reported simultaneous production failures were lock-table
exhaustion from identifier advisory locks retained in a large resolver
transaction, not merely lock-order deadlocks. Whole-file coordination locks
also prevent useful interleaving even when import data commits in batches.

**How to apply:** Preserve bounded bucket locking, bounded resolver commit
groups and transaction-scoped import coordination. Do not make the default-64
PostgreSQL scale regression pass by increasing its server lock budget.