# Segments

## Definition DSL

A segment is a JSON document validated by Pydantic (`app/segments/schema.py`). It is compiled to SQLAlchemy Core with bound parameters. Nothing from the definition is ever interpolated into SQL text.

```json
{
  "version": 1,
  "root": {
    "op": "and",
    "rules": [
      { "type": "trait", "field": "donor_status", "operator": "in", "value": ["lapsing", "lapsed"] },
      { "type": "trait", "field": "ltv_total", "operator": "gte", "value": 250 },
      { "type": "gift", "aggregate": "count", "operator": "gte", "value": 1,
        "where": { "fund": { "operator": "eq", "value": "Missions" } },
        "window": { "kind": "between", "from": "2023-01-01", "to": "2024-12-31" } },
      { "type": "event", "name": "Video Watched", "aggregate": "count", "operator": "gte", "value": 3,
        "window": { "kind": "last_days", "days": 30 } },
      { "type": "consent", "channel": "email", "status": "opted_in" },
      { "op": "or", "rules": [
          { "type": "profile", "field": "region", "operator": "in", "value": ["TX", "OK"] },
          { "type": "enrichment", "source": "zeta_enrichment", "field": "hh_income_band", "operator": "in", "value": ["100-150k", "150k+"] }
      ]},
      { "type": "segment", "segment_id": 12, "operator": "not_in" }
    ]
  }
}
```

### Node types
| Type | Fields | Compiles to |
|---|---|---|
| group | `op` (`and`, `or`), `rules`, optional `negate: true` | Nested boolean expression |
| profile | `field` from the profile field registry | Predicate on `active_profiles` |
| trait | `field` from the trait registry | Predicate on `profile_traits` (left join) |
| gift | `aggregate` (`count`, `sum`, `max`, `min`, `exists`), optional `where` on `fund, campaign, appeal_code, channel, is_recurring, amount`, optional `window` | Correlated `EXISTS` or aggregate subquery on `gifts` |
| event | `name`, optional `where` on `properties.<key>` (text equality only), `aggregate` (`count`, `exists`), `window` | Subquery on `events` restricted by partition-friendly `occurred_at` range |
| consent | `channel`, `status` | Predicate on `consents` (missing row = `unknown`) |
| enrichment | `source`, `field` from `enrichment_attributes` | Subquery on `enrichment_values` using the typed value column |
| source | `source_key`, `operator` (`in`, `not_in`) | `source_keys` array containment on `profile_traits` |
| segment | `segment_id`, `operator` (`in`, `not_in`) | Membership in the referenced segment's materialized membership. Cycles are rejected at save time |

### Operators by data type
| Type | Operators |
|---|---|
| text | `eq, neq, in, not_in, contains, starts_with, is_null, is_not_null` |
| number | `eq, neq, gt, gte, lt, lte, between, is_null, is_not_null` |
| date / timestamp | `before, after, between, in_last_days, not_in_last_days, is_null, is_not_null` |
| boolean | `is_true, is_false` |
| enum | `in, not_in` |

`contains` and `starts_with` escape `%` and `_` in user input.

### Windows
`{"kind": "last_days", "days": N}`, `{"kind": "between", "from": date, "to": date}`, `{"kind": "before", "date": date}`, `{"kind": "after", "date": date}`, or omitted (all time). All relative windows resolve against an `as_of` timestamp passed into the compiler.

## Field registry (`app/segments/registry.py`)

A single registry lists every field the builder can use: key, label, group (`Profile`, `Giving`, `Engagement`, `Consent`, vendor name for enrichment), data type, allowed operators, enum values (static, or a callable that runs `SELECT DISTINCT ... LIMIT 200` for fields like `fund` or `campaign`), and the SQLAlchemy column. The compiler rejects any field that isn't in the registry. The API exposes the registry at `GET /api/segments/fields` so the builder renders from it.

## Compiler (`app/segments/compiler.py`)

- `compile(definition, as_of) -> Select` returning `SELECT active_profiles.id`.
- Max depth 5, max 50 rules total. Validation errors return a path to the offending node (`root.rules[2].rules[0]`).
- Rules become `EXISTS` subqueries where that's cheaper than joins. Aggregate rules use a grouped subquery joined on `profile_id`.
- Count preview runs `SELECT count(*) FROM (<compiled>)` inside a transaction with `SET LOCAL statement_timeout = '10s'`. Timeout returns a friendly error suggesting a narrower window.
- Sample returns 25 profile IDs with masked or unmasked fields depending on role.
- `EXPLAIN` output is available to admins in the builder (collapsed panel) for debugging slow segments.

## Materialization

- `segment.materialize` job: run compiled query into a temp table, compute adds and removes against current membership, replace membership in one transaction, insert `segment_counts`, update `last_count` and `last_materialized_at`.
- Triggers: on save (if active), on schedule (`refresh_schedule`, default daily 04:00 after traits), and before every activation run.
- A segment that references another segment materializes its dependencies first (topological order).

Schedules use canonical `daily` (04:00 UTC) or `null` (manual); the legacy
`0 4 * * *` input is normalized to `daily`, not treated as arbitrary cron.
Scheduled refreshes require their day's successful nightly full-traits job.
Pending traits defer the refresh; failed/cancelled/missing nightly work fails
explicitly for operator repair and retry. Manual refreshes also reject the latest
failed/cancelled full recompute. Refresh and recompute share a transaction advisory
lock through the membership commit.

Delta previews remove the selected constraint and prune newly empty ancestors,
including negated and OR groups; remaining groups retain their boolean semantics.
If the root disappears, the comparison is all active partners. Empty groups are
still invalid for saved definitions.

### Deploying migration 0024

Pause API writes and workers, let in-flight audience transactions finish, then run
`alembic upgrade head` in the normal backend deployment environment. The migration
uses transaction-local `lock_timeout='5s'` before its segment-table DDL. If a reader
or writer prevents the lock, deployment must fail and roll back rather than wait
indefinitely: identify/drain the blocking session and retry the same migration.
Confirm `/readyz` reports the current head before resuming workers and API traffic.
Do not bypass the lock timeout or force-terminate production sessions blindly.

## Builder UI

- Tree editor: add rule, add group, toggle AND/OR, negate group, drag to reorder, duplicate, delete.
- Field picker with search and groups from the registry; operator and value inputs adapt to type (multi-select with typeahead for enums, number inputs, date pickers, relative-day inputs).
- Live count (debounced 800 ms) and a count delta per rule ("removing this rule would add 12,340").  The per-rule delta can be computed lazily on hover to save queries.
- Side panel: sample of 25 profiles, breakdown by donor_status and by source.
- Human-readable summary rendered from the JSON ("Lapsing or lapsed donors with lifetime giving ≥ $250 who …").
- JSON view (read-only for analysts, editable for admins) for copy/paste between environments.
- Save as draft or active; duplicate segment; archive.

## Required tests

- Every operator per data type compiles and returns the correct rows on a fixed fixture.
- Nested groups with negation.
- Unknown field, wrong operator for type, bad enum value → validation error with node path.
- Injection attempts in values (`'; DROP TABLE`, `%`, `_`) are treated as literals.
- Relative windows are deterministic with a pinned `as_of`.
- Segment-in-segment and cycle rejection.
- Consent rule with missing consent row behaves as `unknown`.
- Enrichment rule excludes expired values.
- Materialize computes adds/removes correctly across two runs.

## Benchmark modes

```sh
# Default: existing app database, read-only, all 23 current template definitions
kinship benchmark --segments
kinship benchmark --segments --as-of 2025-01-01

# Explicit synthetic exception: private schema or local private database
kinship benchmark --segments --isolated --profiles 50000 --scale small

# Explicit fresh audit exception: all 23 identity/materialization checks
kinship benchmark --audit-segments --profiles 500000 --scale medium
```

The default `--segments` path uses the configured application database, including
a valid `APP_ENV=production` configuration. It bypasses the **isolated benchmark**
dispatch/production guard, not application authentication or configuration
validation. `KINSHIP_BENCHMARK_BACKEND=schema` does not change that default.
No production query is required to test dispatch.

The read-only runner creates a dedicated, unpooled connection without application
worker-registration hooks. Startup sets `default_transaction_read_only=on`;
`SET TRANSACTION READ ONLY` precedes the first application query, followed by
repeatable-read isolation. One UTC reference instant and one snapshot cover the run.
The transaction is always rolled back and the connection disposed on failure.
It performs no seeding, migrations, template installation/updates, temporary
tables, materialization, job enqueueing, audit writes or exports. PostgreSQL
rejects attempted INSERT/UPDATE/DELETE and CREATE TEMP TABLE, not just a Python
convention. Other application writers can still change data outside its snapshot.

The report is `segments.json` under a unique `segments-readonly-*` directory in
`KINSHIP_BENCHMARK_OUTPUT` or `--output-dir`. It records actual total and active
profile counts; the default or explicitly supplied `--profiles 500000` **does not**
resize or describe existing data and is recorded as ignored. Each of the 23
code-defined templates has a count, count/preview/sample `first_ms`, three
`warm_ms` values and `warm_median_ms`, with materialization explicitly marked
`skipped` because the run is read-only. Empty audiences are legitimate.
Saved/edited application definitions are not overwritten or installed.

Count timings execute the compiled count query; preview timings also compile;
sample timings include the API-equivalent 25-row read and full status/source
breakdown SQL, but exclude HTTP transport. No sample contact fields are saved.
“First” is the first execution for that operation/template, **not** cold OS or
PostgreSQL buffers; count precedes preview and sample. Per-statement timeouts
are 10 seconds. A template error is explicit in the report and returns a failing
exit status; other templates are still attempted through read-only savepoints.

`--seed-dir`, `--scale`, import row-count flags and profiling/test filters are
rejected with read-only `--segments` (even if a supplied value equals a default).
Use `--isolated` to opt into synthetic preparation. Other benchmark modes cannot
be combined with segments. `--segments --isolated` preserves the historical
six-template synthetic benchmark; `--audit-segments` is an explicitly named
fresh-data exception, defaults to 500,000 people, and does not require an extra
`--isolated` flag. Its seeding, migration and materialization writes are confined
to the generated private schema/database. Schema/role and generated seed data
are cleaned up after workload failure or handled interruption; cleanup failures
are surfaced as failed receipts, never claimed successful. Local private mode
still refuses `APP_ENV=production`. Shared-server schema mode retains its
restricted-role privilege checks; neither mode flushes caches or restarts the
application database. SIGKILL/host loss cannot guarantee cleanup.

Safety tests: `test_readonly_benchmark.py` uses no database; the private-socket
`test_readonly_benchmark_pg.py` verifies write and temp-table refusal, unchanged
durable table counts for the full run, and that transaction protection precedes
queries. Run PostgreSQL tests only through the existing disposable test harness,
never with the running application's database URL.

## Historical six-template isolated benchmark

1. Lapsed donors, LTV ≥ $500 (reactivation).
2. New donors in the last 90 days (welcome series).
3. Active recurring donors.
4. Top 5% by 12-month giving (major donor pipeline).
5. Viewers with 3+ video views in 30 days who have never given (conversion).
6. Email opted-in, no gift in 12 months, gave 2+ times before (lapsing save).
