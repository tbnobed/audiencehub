---
name: Dashboard performance during imports
description: Why dashboard performance acceptance must include cold calls during changing data
---

Precomputed dashboard facts and shared stale-while-revalidate responses are an
explicit product decision; do not restore live raw-table aggregation for fresher
numbers.

**Why:** On 2026-09-25 the user explicitly chose nightly/post-import rollups and
immediate shared-cache delivery after repeated production latency failures.
This supersedes the earlier live-committed-read approach.

**How to apply:** Missing rollups require an offline backfill, not a request-time
scan. Preserve this boundary when adding cards, CSV exports or new filters.

Judge dashboard responsiveness by cold computation under concurrent writes, not
only by cached responses or small correctness fixtures.

**Why:** The Overview remained on loading placeholders at multi-million-row
production scale although its small-fixture tests and API health checks passed.
Correctness-driven cache invalidation can erase cache benefits while imports,
identity resolution, or job heartbeats continue committing.

**How to apply:** Preserve resolved-profile and consent semantics when optimizing.
Measure the cold endpoint at realistic gift/source cardinalities, explicitly
distinguish synthetic concurrent updates from the actual resolver workload, and
keep backend computation deadlines shorter than frontend request deadlines.
Never apply dashboard read timeouts indiscriminately to large upload writes.

Validate deadline failures through the full HTTP dependency teardown and the next
pooled request, not only by calling endpoint functions directly.

**Why:** Production deadline errors were replaced by rollback cancellation during
session cleanup; function-level timing tests did not exercise that lifecycle.
Also keep bound HMAC key material out of SQL exception parameter logs.