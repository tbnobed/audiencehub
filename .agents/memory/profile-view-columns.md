---
name: PostgreSQL profile view columns
description: SELECT-star views freeze column lists; schema changes must refresh dependent views
---

Treat view column lists as stored schema, not dynamic SELECT-star projections.
Any profile/trait column addition must refresh full-row application views and
preserve dependencies, materialized-view indexes, filters and permissions.

**Why:** A physical profile column was added and backfilled successfully while
active_profiles retained its old projection, breaking compiled audiences only
on production. Correct table DDL alone did not establish view compatibility.

**How to apply:** Keep the migration-chain post-revision guard in use. Check the
view's full column signature against the base table; use dependency-ordered,
transactional recreation rather than DROP CASCADE. Preserve intentionally
explicit custom projections rather than automatically exposing new fields.
Runtime verification remains on the user's server.
