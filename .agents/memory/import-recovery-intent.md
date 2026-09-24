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