---
name: First-party identity boundaries
description: Preserve accepted person identity semantics while extending matching, households and devices.
---

Keep every existing deterministic identity rule, key type and Master ID behavior
unchanged when extending the first-party graph.

**Why:** The user explicitly required preserving the accepted identity system,
not replacing it with a new matcher.

**How to apply:** Probabilistic matching is a separate, opt-in batch path using
the existing winner ordering and reversible merge journal. Never merge two
different values of any system key through probabilistic matching. Households
group people without merging them. Device observations require an authenticated
originating server event with an Okta identity; public SDK credentials and JSON
authentication flags are not sufficient. Device IDs must never become person
resolver bridges.

Use isolated PostgreSQL scratch for matching and include abandoned scratch data
in privacy erasure.

**Why:** Splink work tables contain identifying data even though they are outside
the application's regular table inventory. A killed process can leave them.

**How to apply:** Coordinate cleanup with the existing privacy/resolver locks.
When an application transaction reads scratch tables, drop them on the same
transaction or after it ends; another connection's DROP would wait on the
reader's own access-share locks.
