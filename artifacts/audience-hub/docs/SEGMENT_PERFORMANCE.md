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

## Long-window donation follow-up

Migration `0037_last_donation_at` adds `profiles.last_donation_at` and a concurrent
btree index on `(last_donation_at,id)`. It backfills the existing active profiles
in independently committed batches of 5,000, logs progress, analyzes profiles,
and can be retried after interruption. Use the same stopped-writer, single
migration process rollout above; no reset, reseed or reimport is needed.

The full/incremental traits pipeline refreshes this timestamp in the same
transaction as its other projections. It is the latest **donation event's
occurred_at**, not a date reconstructed from gifts or an imported last-gift
property. Donation events need not have a mirrored gift to qualify. Explicit
returns from either event or gift evidence exclude only their referenced
donation in the same profile and source. Both original-reference keys are
recognized, including the raw event message ID and existing `gift:`/`donation:`
ledger namespaces. Unlinked returns do not invent associations. If every
donation is returned, the timestamp becomes null; a return of the latest donation
falls back to the prior unreturned donation. Any explicitly linked return marks
that donation as returned; this summary does not redefine ledger amounts or
partial-refund accounting.

Unfiltered `event donation / exists / is_true|is_false` rules with `previous_days`
or `last_days` windows use indexed timestamp ranges. For example the 720-day
TBN+ donor/non-donor rules no longer probe donation history for ordinary rows.
Null timestamps satisfy the non-donor side, and group negation is preserved.

A maximum timestamp alone cannot answer arbitrary historical windows: a newer
donation today or in the future could hide an older qualifying donation. Only
when the maximum is at/after the window's exclusive end does the compiler use a
bounded, non-returned donation history check. This also preserves pinned `as_of`
queries without requiring another backfill. Normal rows use the range branch;
benchmark plans will show whether the exceptional subplan was executed.

Rules with event-property/resource filters (including source code, amount and
fund), non-existence aggregates, other event names, and other window types keep
their event-table path. The fixed Giving Tuesday audience is not compiled from
the latest-donation timestamp. The existing read-only 23-audience benchmark
automatically captures plans for the new branch.

Regression coverage in `test_last_donation_pg.py` covers event-only donations,
event/ledger returns, source isolation, nulls, incremental clearing, inclusive
start/exclusive end, future/today/historical cases, and filtered-rule fallback.
It was added as code only and was **not run in Replit**. Performance and count
parity still require the production-server verification run.

## Profile-view column repair

PostgreSQL freezes a view's `SELECT *` column list at creation. Migration `0037`
added the physical `profiles.last_donation_at` column but did not expand
`active_profiles`. Migration `0038_refresh_profile_views` corrects that omission.
The repository defines only one affected application view, `active_profiles`;
there are no application-defined materialized views on profiles/traits.

The repair discovers additional profile/trait-backed views and transitive view
dependencies through PostgreSQL catalogs. It drops them in reverse dependency
order **without CASCADE**, then recreates them in dependency order in the same
transaction. `active_profiles` is recreated with the complete physical profile
column list and its unchanged filter:

```sql
WHERE merged_into_id IS NULL AND NOT is_deleted
```

Other views retain their explicit definitions/projections, not an invented
expansion that might expose sensitive data. Materialized views retain populated
versus unpopulated state, their access method/storage settings, tablespace and
index definitions. Owners, relation/column permissions, comments and clustered
index choices are restored. A populated materialized view is rebuilt from its
query; budget the corresponding time/space if custom ones exist. Custom view
triggers/rules are refused before dropping anything; other unsupported
dependencies cause the non-CASCADE drop to fail and the transaction to roll
back. DDL lock waits are limited to five seconds.

The Alembic migration chain now snapshots the physical `profiles` and
`profile_traits` column signatures and checks them after every revision. When
either changes, it rebuilds these dependent views automatically; it also repairs
a stale `active_profiles` column list. The final invariant checks column names,
order, types, type modifiers and collations against `profiles`. This callback
applies to normal online `alembic upgrade`/`downgrade` execution, not ad hoc SQL
run outside Alembic. Future migrations must not use DROP CASCADE to work around
view dependencies or bypass this chain.
Column removals/type changes that PostgreSQL refuses because of an existing
dependency still need an explicit pre-DDL view teardown in their own migration;
a post-revision callback cannot repair a statement that never completed.

Use the same stopped-writer, single-migration rollout commands above. **No
reset, reseed, imports, index rework on the physical profile table, or backfill
rerun is needed.** Migration 0038 changes the views, not the previously computed
timestamps or physical profile/traits rows.

Added server-side tests in `test_profile_view_migrations_pg.py` cover the repaired
column list, future profile/trait additions, unchanged active/deleted filtering,
dependent views, populated/unpopulated materialized views, indexes and metadata.
They have not been executed in Replit.
