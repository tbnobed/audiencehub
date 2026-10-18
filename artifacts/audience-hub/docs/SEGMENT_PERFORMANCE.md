# Segment property performance rollout

## Diagnosis and choice

The previous compiler selected each canonical property using a correlated
source-record subquery, ordered by source priority, record update time and ID.
Indexing JSONB alone cannot eliminate that per-profile winner selection, and
filtering source records before choosing a winner would change audience counts.

Migration `0036_segment_properties` adds nullable typed imported-property columns
to `profile_traits`, distinct from computed Kinship RFM/giving fields. Every
canonical field used by the 23 audited audiences is covered, including the
additional donor-account and lifetime evidence. Other catalog properties retain
their existing source-record compilation path.

Selection preserves lowest source priority, newest record, lowest record ID,
independently per field. JSON nulls are skipped, false/zero are retained, and
blank date/number/timestamp winners become null without falling through to a
lower-priority record. Imported fields continue to use imported evidence, not
computed traits. No identity keys, overlaps, profile IDs, resolver logic or seed
proportions change.

`last_activity_at` is the greatest canonical `last_seen`/`last_contact`, maintained
with the projection by full and incremental trait recomputation. The compiler
adds an indexed activity lower bound to the existing 120-day OR and retains its
two original predicates. This preserves the audience when one side has a future
timestamp but the other side is inside the historical window.

Every projected field has a btree index ending in profile ID; text fields use
`text_pattern_ops` for equality and prefix rules. Broad audiences may correctly
use sequential scans. Index maintenance and the wider traits row have storage
and write costs; production plans determine whether further composite indexes
are justified. Source-priority edits queue affected profiles for recomputation.
Imported properties therefore refresh with the existing asynchronous traits
pipeline; wait for tracked traits completion before acceptance measurements.

## Sample and count contract

`POST /api/segments/sample` only executes the compiled ordered `LIMIT 25`, then
hydrates those IDs. It does not count membership or aggregate breakdowns.
`POST /api/segments/breakdowns` retains the full-audience status/source
aggregation. The UI displays rows first and loads breakdowns separately, with
independent loading/error messages.

The live count still comes exclusively from `/preview`; the UI reuses that
query result for 15 seconds for the identical definition. No cross-user,
cross-process or unbounded server count cache is introduced. Sample timing does
not include optional breakdowns or HTTP transport.

## Server rollout — no reset or reseed

Take/retain a database backup. Use a persistent terminal session. Stop API and
worker writers and run exactly one migration process before starting either
upgraded service:

```bash
git pull --ff-only
docker compose build api
docker compose stop api worker
docker compose run --rm --no-deps api sh -c 'cd /app/backend && alembic upgrade head'
docker compose up -d api worker
```

Do not run the migration simultaneously from multiple service startups.
The migration:

- Adds nullable columns without a whole-table default rewrite.
- Commits each 5,000-profile backfill independently, logging the last profile ID.
- Leaves existing computed trait values unchanged.
- Creates indexes concurrently; cancelled invalid indexes are rebuilt on retry.
- Analyzes the table after backfill/index creation.
- Uses a five-second lock timeout instead of waiting indefinitely for DDL locks.

This migration deliberately commits partial progress. If interrupted, leave
writers stopped and rerun the migration: columns and indexes are guarded and the
backfill is idempotent (it starts from the beginning). Do not start the upgraded
services until the migration finishes. Keep sufficient free disk for indexes
and backfill WAL; elapsed time and disk usage have not been measured here.

## Production verification

No tests, builds, seeds, imports or benchmarks were run in Replit for this change.
New server-side regression coverage is in `test_segment_projection_pg.py`;
existing API and audited-evidence tests have been updated for the split sample
contract. Execute database tests only against an isolated test database, never
the production dataset.

After the traits pipeline is caught up, run against the existing production
data (no seed generation or materialization):

```bash
docker compose exec -T api kinship benchmark --segments
```

The PostgreSQL-enforced read-only runner saves `segments.json` and a
`<template-key>-plans.json` file for each of the 23 audiences in its printed
evidence directory. Each plan file contains bound count SQL and ordered-limit
sample-ID SQL with `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`. Plan capture follows
timings to avoid warming the first measured operation. Hydration of the selected
25 profiles is included in sample timing but not the membership plan.

Compare all counts against the accepted run and retain both sets of evidence.
The report records under-2s count and under-1s sample target booleans at the actual
population. Correctness PASS does not imply performance PASS or extrapolate
500k performance to 8M. EXPLAIN ANALYZE executes reads and consumes resources;
the runner retains its existing statement timeout and never disables read-only
protection. First execution is not a cold OS/PostgreSQL-cache measurement.
