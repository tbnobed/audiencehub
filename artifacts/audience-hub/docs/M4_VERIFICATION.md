# M4 verification

**Current resolver result: full 500k-person contacts-only resolution completed;
the under-ten-minute target was narrowly missed.** Final unprofiled resolution
took **626.745 seconds** for 1,250,000 source records, producing 492,822 profiles.
The old bounded baseline resolved only 32,000 records in 121.223 seconds. These
are different completion scopes: no exact full-run speedup is claimed.
Historical five-source M4 evidence remains in `m4-verification-results.json`;
the resolver-first measurements below supersede its unresolved-identity timing
gap, not its unmeasured full-medium trait run.
New compact tracked evidence: `identity-resolution-verification-results.json`.

| Final measurement | Actual | Result |
|---|---:|---|
| 500k-person contacts-only full resolution | 626.745 s | Complete; **fails** <600 s |
| 50k-record incremental resolution | 120.377 s | Complete; **fails** <60 s |
| 250k-active-profile full trait handler | 366.165 s | Complete; correctness passed |
| Same 500-person ground-truth sample, before/after | 0 non-household false merges; 3 split people | Metrics unchanged |

The user-approved **250k trait fallback was actually completed**. A full-medium
five-source trait timing was **not** run in this session. The two resolver
latency targets remain acceptance gaps; this report does not claim M4 fully
passes.

## Resolver-first baseline (contacts only)

The new `scripts/identity_benchmark.py` runs only donor CRM contacts, ESP contacts,
and Zeta enrichment through the real mapping/validation/import service. Gifts and
events are **not imported**. It creates a disposable PostgreSQL 17.5 cluster with
`pg_stat_statements` preloaded, TCP disabled, a `/tmp/kinship-bench-*` socket, and
database files on the workspace volume. Neither application nor production
database credentials are used. The observed container quota is 4 CPUs / 8 GiB;
PostgreSQL settings were 512 MB shared buffers and 32 MB work memory.

The old resolver and survivorship were frozen before engine edits, with SHA-256:

* resolver: `3407f3d9e3a43cefb3a2e85713ef6d5b2df84e99fdf4470ff1ff677eb2149086`
* survivorship: `ae731c1d29afdb036aaa30b55b6bf77976e2ba9f291f5e549fe53aaa79b14f29`

The medium fixture is the existing deterministic 500,000-person CSV seed
(`random_seed=20250308`). Real imports took **253.254 seconds** including setup
inside the loader child and produced **1,250,000 source records, zero gifts,
zero events, zero pre-existing profiles**. CSV row counts were 504,952 CRM,
251,445 ESP and 500,002 Zeta; each rejected its single intentionally bad row.
Duplicate rows were deduplicated by the importer.

**Before, deadline-bounded:** 121.223 seconds profiled wall time, 64 committed
groups, 32,000 resolved records, 31,959 profiles created, zero merges. This is
only **2.56%** of the source records: completion is false. There is no measured
full-resolution time and no claim that the ten-minute target passes. The
120-second deadline is checked between transactions, not a SQL timeout.
Import setup and database cloning are excluded from resolver timing.

### cProfile top 20, before (cumulative seconds)

| Function | Calls | Cumulative seconds |
|---|---:|---:|
| resolver.resolve_batch | 64 | 105.261 |
| psycopg connection.wait | 1,295 | 82.266 |
| Session._execute_internal | 975 | 67.308 |
| Session.execute | 911 | 67.245 |
| elements._execute_on_connection | 975 | 67.232 |
| Connection._execute_clauseelement | 975 | 67.229 |
| Connection.execute | 911 | 67.199 |
| Connection._execute_context | 975 | 67.143 |
| Connection._exec_single_context | 975 | 67.063 |
| cursor.execute | 1,039 | 66.963 |
| default.do_execute | 975 | 66.951 |
| _cursor_base._execute_gen | 31,509 | 31.367 |
| _cursor_base._convert_query | 1,039 | 30.432 |
| _queries.convert | 1,039 | 30.423 |
| _queries.dump | 1,039 | 30.413 |
| array.dump | 1,871 | 24.684 |
| array.dump_list | 1,871 | 23.898 |
| resolver._auto_block_high_cardinality | 64 | 23.646 |
| resolver._blocked | 64 | 22.724 |
| state_changes._go | 1,167/1,039 | 15.362 |

These overlapping cumulative times must not be summed. There were 123,807,778
function calls. The current discovery loop fetched 640,000 rows while committing
only 32,000: repeated 10,000-row discovery and Python array parameter conversion
are material costs. `array.dump_list` alone used 9.534 seconds self time.

### pg_stat_statements top 10, before

Sorted by total execution time, scoped to this private database, reset immediately
before resolution. SQL labels below summarize the full normalized statements in
the retained JSON; timings are PostgreSQL execution only, not client adaptation.

| Statement | Calls | Total ms |
|---|---:|---:|
| Email high-cardinality count distinct by source | 64 | 14,808.583 |
| identifiers bulk upsert | 64 | 3,738.739 |
| Phone high-cardinality count distinct by source | 64 | 3,281.506 |
| Existing identifier lateral lookup | 64 | 2,934.876 |
| Blocklist lateral lookup | 64 | 2,625.933 |
| source_records assignment update | 61 | 2,311.288 |
| Pending source-record discovery | 64 | 2,108.712 |
| Profile surviving-field update | 64 | 1,809.545 |
| Dirty-profile upsert | 64 | 281.066 |
| New profile insert | 64 | 229.548 |

The email guard read 680,203 shared blocks and wrote 32,710 temporary blocks.
Set-based guards/edge construction should avoid repeating these checks for
each 500-record commit. Survivor updates were not the dominant SQL cost here.

### Ground-truth baseline scope

A fresh **500-person small-scale seed**, random seed 20250308, was fully resolved
before changing the engine: 1,250 contacts/enrichment records in **1.164 seconds**
with cProfile enabled. Result: 493 active profiles, 10 shared-household profiles,
**zero non-household false-merge candidates**, and **3 split people**. All three
splits have junk CRM email plus unusable normalized CRM phone and a distinct ESP
email. This is a deliberately small contacts-only accuracy sample, **not** a
rerun of the previous 50,000-person five-source acceptance fixture. Ground truth
also contains generated gifts/events, but those were excluded from imports:
1,250/6,389 truth records were observed. Compare the same seed and source scope
after optimization; do not interpret excluded truth records as resolver misses.

The final tuned engine was rerun against an untouched clone of this exact
small fixture: **ground-truth acceptance metrics are unchanged** (493 active profiles,
10 intentional households, zero other false-merge candidates, three split
people, all 1,250 imported truth records found). Final small resolution took
1.764 seconds unprofiled. That timing overlapped medium fixture cloning and
is an accuracy smoke, not a performance acceptance result.

### Final full-medium acceptance timing

Final engine SHA-256:
`3823f06925d1dfb06140f0fe642075239d86a2a27a57286700e97982271d252f`.
`resolve_bulk(db)` plus commit and the final empty completion check were timed
without cProfile: **626.745486 seconds**, complete, two committed calls,
1,250,000 records, 492,822 profiles created, zero merges. The under-600-second
target **fails by 26.745 seconds (4.46%)**; no further tuning was applied.
Fixture imports, private server startup, database cloning and snapshots are
outside the resolver timer. PostgreSQL's statement-statistics extension remains
loaded, but SQL statistics extraction/reset/report formatting and cProfile are
not in this acceptance interval.

A preceding diagnostic run, before the final indexed-empty-probe and
propagation-memory tuning, completed the same full fixture in **647.180 seconds
with cProfile enabled**. It is diagnostic evidence, not the final target time.
Python work fell to 26,197 calls; the remaining wall time is dominated by
PostgreSQL. Final tuning uses 256 MB work memory during propagation and restores
64 MB afterward. Server settings remain 512 MB shared buffers / 32 MB base work
memory on the measured 4-CPU, 8-GiB quota.

#### Full-run diagnostic cProfile top 20 (pre-final tuning)

| Function | Calls | Cumulative seconds |
|---|---:|---:|
| psycopg connection.wait | 87 | 647.052 |
| bulk.resolve_bulk | 2 | 644.493 |
| Session._execute_internal | 77 | 644.484 |
| bulk._sql | 75 | 644.475 |
| elements._execute_on_connection | 77 | 644.471 |
| Session.execute | 75 | 644.471 |
| Connection._execute_clauseelement | 77 | 644.471 |
| Connection.execute | 75 | 644.462 |
| Connection._execute_context | 77 | 644.456 |
| Connection._exec_single_context | 77 | 644.447 |
| cursor.execute | 79 | 644.435 |
| default.do_execute | 77 | 644.435 |
| bulk._survivorship | 1 | 83.993 |
| bulk._materialize | 1 | 75.001 |
| state_changes._go | 83/79 | 2.690 |
| Session.commit | 2 | 2.686 |
| string wrapper commit | 2 | 2.686 |
| SessionTransaction.commit | 2 | 2.686 |
| Transaction.commit | 2 | 2.618 |
| RootTransaction._do_commit | 2 | 2.618 |

#### Full-run diagnostic pg_stat_statements top 10 (pre-final tuning)

| Statement | Calls | Total execution seconds |
|---|---:|---:|
| source_records assignment UPDATE | 1 | 139.987 |
| Identifier-key minimum-label aggregation | 4 | 118.909 |
| Surviving-field CTE/profile UPDATE | 1 | 74.087 |
| identifiers INSERT/upsert | 1 | 59.623 |
| enrichment_values INSERT/upsert | 1 | 56.722 |
| Record-label propagation UPDATE | 4 | 40.657 |
| Pending-record temp-table creation | 2 | 37.643 |
| Normalized-edge INSERT | 1 | 25.941 |
| Dirty-profile INSERT/upsert | 1 | 12.707 |
| Merged-enrichment temp-table creation | 1 | 9.670 |

The final one-time tuning addressed the empty pending scan and propagation
spilling. The largest remaining opportunity is the 1.25M-row indexed assignment
rewrite and its WAL/I/O costs, plus survivor/enrichment materialization. This is
a profiling observation, not a claim that an unmeasured change meets the target.

### Evidence and repeat commands

Local raw evidence is retained under `.cache/m4-verification/before-medium/`
and `before-small/`: `resolver.prof`, `cprofile-top20.txt`,
`pg-stat-statements-top10.json`, `resolver-profile.json`, `imports.json`,
`load.json`, and (small only) `accuracy.txt`. Frozen modules are in
`.cache/m4-verification/frozen-identity/`. These private local paths are not
release artifacts. An unresolved database template is retained for matching
after tests. The medium PostgreSQL server was explicitly stopped after capture.

From the workspace root, with PostgreSQL binaries and backend Python available:

```sh
python artifacts/audience-hub/scripts/identity_benchmark.py \
  --output /large-volume/identity-before \
  --seed-dir .cache/m4-verification/medium-seed \
  --frozen .cache/m4-verification/frozen-identity --seconds 120
# Omit --frozen to run the current engine; use a fresh --output directory.
```

Run this in a persistent terminal: import + profiling exceeds a five-minute
process-tree timeout. `--load-only` allows stopping after imports for a separately
invoked `--phase resolve` child configured with the private fixture environment.
The runner intentionally retains the cluster for comparison; stop it explicitly
with `pg_ctl -D OUTPUT/postgres -m fast stop`. Private environment JSON contains
only benchmark-generated configuration and has mode 0600; never commit it.
An initial medium attempt hit the shell's five-minute limit while cloning, before
profiling evidence was written. That dataset was reimported; the successful
numbers above are a separate fresh run, not combined timing from the attempt.

The final full and incremental resolver measurements below supersede earlier
pending timing statements. Neither requested resolver latency target passed.

### Prepared incremental and 250k-trait follow-up

`scripts/identity_followup_benchmark.py` prepares and measures follow-ups only
against private cloned databases named `followup_*`. It refuses application TCP
connections, non-test settings, and the original `benchmark`/template databases.
The final optimized full and incremental runs use the same engine hash recorded
above and are explicitly unprofiled acceptance measurements.

The 50,000-record fixture has actually been prepared at
`.cache/m4-verification/incremental50k/incremental-esp.csv` from the first 50,000
distinct real ESP CSV external IDs (SHA-256
`ba90f1840ce4ae01bd0e13862ed9074e984de61595d111c5073cd7536add608b`).
It creates a new source importing existing identities, exercising touched existing
components rather than inventing 50,000 new people. Import and resolution are
timed separately. The importer requires exactly 50,000 unresolved records and no
rejections before accepting the setup. After the full new resolver completes,
clone its resolved database as `followup_incremental`, then:

```sh
# DATABASE_URL and all other settings must come from the PRIVATE fixture env.
# Change only the fixture URL database name to the clone, not its socket.
python artifacts/audience-hub/scripts/identity_followup_benchmark.py incremental-import \
  --seed-dir .cache/m4-verification/medium-seed \
  --output .cache/m4-verification/incremental50k
python artifacts/audience-hub/scripts/identity_benchmark.py --phase resolve \
  --seed-dir .cache/m4-verification/medium-seed \
  --output .cache/m4-verification/incremental50k --seconds 60
```

The resolver deadline still checks between transactions; an overlong first
transaction is reported at its actual duration, never silently called a pass.
Capture the `complete` field as well as elapsed time.

**Actual final incremental result:** real import took **44.781832 seconds**
(excluded from resolution timing), accepted all 50,000 rows with zero rejections
and exactly 50,000 pending records. Resolution then completed in
**120.376768 seconds unprofiled**, two committed calls, 50,000 resolved records,
zero new profiles and zero merges. All imported identities therefore reused
existing profiles. The under-60-second target **fails by 60.377 seconds**.
It is not relabeled a pass by omitting survivorship/materialization/commit.

For the user-approved 250k-trait fallback, independently clone the **fully
resolved 500k contacts-only database** as `followup_traits`. Select the lowest
250,000 active profile IDs and mark other profiles deleted in that clone. Thus
the trait population is exactly 250,000, while inactive profiles and the original
1,250,000 contact/enrichment source records remain; record both total and active
counts. This is not a claim to a freshly generated 250k-person fixture.

Fixture setup streams the **actual medium gift/event CSVs** into typed PostgreSQL
COPY staging tables, then inserts with deduplication. Gifts link through the
actual CRM external-ID-to-resolved-profile mapping. Event links use the verified
seed generator's `call-{person-number}-{call-number}` convention to find that same
CRM mapping. Only selected-profile facts are inserted. Actual date parsers,
decimal amounts, recurrence flags and JSON event properties are retained; invalid
selected values and out-of-scope rows are counted explicitly.

This deliberately bypasses the slow real gift/event import pipeline **for trait
fixture setup only**, not for the contacts-only resolver benchmark. It does not
create gift/event source-record rows or claim importer correctness/throughput.
The setup timings, copied/inserted/rejected counts and selection scope are in
`traits-setup.json`.

```sh
python artifacts/audience-hub/scripts/identity_followup_benchmark.py traits-prepare \
  --seed-dir .cache/m4-verification/medium-seed \
  --output .cache/m4-verification/traits250k --profiles 250000
python artifacts/audience-hub/scripts/identity_followup_benchmark.py traits-measure \
  --seed-dir .cache/m4-verification/medium-seed \
  --output .cache/m4-verification/traits250k
```

The measurement invokes the actual `traits.recompute` **full production handler**,
pinning the date to 2024-12-31. Full trait writes, current snapshot, 24-month
historical snapshot backfill, dashboard rollups, dirty cleanup, commit and cache
invalidation are included. It independently compares all 21 registry traits
against raw-row Python aggregation for 1,000 deterministically sampled profiles
and checks incremental global RFM ranks on 17 profiles. Setup is excluded.
Evidence checkpoints are in `traits-measurement.json`; a running/partial result
must not be reported as completed timing.

#### Actual 250k fallback result

The fallback **passed**: **250,000 active profiles**, 2,729,497 actual seed gifts,
90,648 actual seed events, and all 1,250,000 resolved contact/enrichment source
records retained. There are 492,822 total profiles; the other 242,822 are marked
deleted only in the disposable clone. No selected fact had a date/amount/JSON
conversion rejection. Out-of-scope or missing-CRM-reference rows excluded from
setup: 2,631,026 gifts and 87,957 events.

Successful typed setup took **328.758 seconds**, including profile selection and
analysis (gifts 205.818 s; events 7.647 s). This is explicitly **not real-import
throughput**. An initial fixture-selection attempt used a large bound-ID-array
anti-filter and was canceled/rolled back before any setup commit; it was replaced
by an indexed temporary target table and SQL anti-join. The successful setup
timing is not combined with that discarded attempt.

Pinned `as_of`: **2024-12-31**. Actual unprofiled full production handler timing,
including commits, dirty cleanup and cache invalidation: **366.164939 seconds**.

| Included phase | Seconds |
|---|---:|
| Recompute including current snapshot and dashboard rollups | 157.322 |
| Dashboard rollups (nested within preceding row) | 82.517 |
| Historical snapshot backfill | 200.952 |
| Full production handler including commit/invalidation | **366.165** |

These phase timings overlap where noted and must not be summed blindly.
SQL execution totals were 69.728 s trait writes, 201.826 s current/historical
snapshots, and 89.215 s other statements including dashboard work.

Independent Python raw-row reference took **38.851 seconds**, outside the
production timer. **All 21 registered traits matched for 1,000 deterministically
sampled profiles**, with zero discrepancies. Computed timestamps were checked
against the actual handler wall-clock interval. Additional 17-profile incremental
global-RFM verification also passed and was rolled back; its production call
refreshes dashboards again, so verification takes additional time outside the
reported full-handler interval. There are exactly 250,000 trait rows and **24
distinct snapshot months**. Measured database size: 4,725,050,515 bytes.

Raw local evidence: `.cache/m4-verification/traits250k/traits-setup.json` and
`traits-measurement.json`; key results are copied into the tracked compact JSON.
Both private benchmark PostgreSQL clusters were stopped cleanly after the final
measurements. Data files and seeds remain for inspection/reproduction.

### Portable isolated rerun, without touching an application database

Use a persistent terminal on the measurement server, PostgreSQL **17** binaries
on `PATH`, the installed backend Python dependencies, and a sufficiently large
data volume. All paths below are examples; run commands from the workspace root.
Do **not** pass an application `DATABASE_URL`, use `--load` on the seed CLI, or
point `--cluster` at an application PostgreSQL directory.

```sh
# Generation only: no database or application credentials are required.
(cd artifacts/audience-hub/backend && python -m app.cli seed \
  --profiles 500000 --scale medium --random-seed 20250308 \
  --output-dir /large-volume/identity/seed)

# Fresh private cluster, real CRM + ESP + Zeta imports only; then clean shutdown.
python artifacts/audience-hub/scripts/identity_benchmark.py \
  --seed-dir /large-volume/identity/seed \
  --output /large-volume/identity/cluster --load-only

# Clone the untouched imported database; unprofiled full resolution + commit.
python artifacts/audience-hub/scripts/identity_benchmark_reuse.py \
  --cluster /large-volume/identity/cluster --database final_medium \
  --clone-from benchmark --snapshot final_resolved_template \
  --stage resolve --unprofiled --seed-dir /large-volume/identity/seed \
  --output /large-volume/identity/full

# Real 50k import and separately timed incremental resolution.
python artifacts/audience-hub/scripts/identity_followup_benchmark.py incremental-prepare \
  --seed-dir /large-volume/identity/seed --output /large-volume/identity/incremental
python artifacts/audience-hub/scripts/identity_benchmark_reuse.py \
  --cluster /large-volume/identity/cluster --database followup_incremental \
  --clone-from final_resolved_template --stage incremental --unprofiled \
  --seed-dir /large-volume/identity/seed --output /large-volume/identity/incremental

# User-approved 250k-active-profile trait fallback; setup is NOT import timing.
python artifacts/audience-hub/scripts/identity_benchmark_reuse.py \
  --cluster /large-volume/identity/cluster --database followup_traits \
  --clone-from final_resolved_template --stage traits-prepare --profiles 250000 \
  --seed-dir /large-volume/identity/seed --output /large-volume/identity/traits250k
python artifacts/audience-hub/scripts/identity_benchmark_reuse.py \
  --cluster /large-volume/identity/cluster --database followup_traits \
  --stage traits-measure --seed-dir /large-volume/identity/seed \
  --output /large-volume/identity/traits250k
```

Each reuse command reads only its private fixture configuration, starts the
private socket server, runs its specified phase, and stops it in `finally`.
Use fresh database names on reruns; the helper does not silently overwrite
existing clones. An interrupted process may need an explicit private-cluster
restart; running each phase in a persistent terminal avoids external five-minute
process-tree termination. The retained `private-env.json` is mode 0600 and must
never be committed or shared.

For **full-medium traits using all five real imports**, rather than the typed
250k fallback, the original isolated verification command remains available:

```sh
cd artifacts/audience-hub/backend
python -m app.benchmark --traits --profiles 500000 \
  --seed-dir /large-volume/identity/seed --output-dir /large-volume/full-m4
```

`app.traits.verification` now invokes the normal bulk identity handler payload
`{}`; it no longer supplies an explicit limit that forces the legacy engine.
This full five-source command may still spend substantial time importing gifts;
that import cost is separate from the measured full trait handler.

The helpers have been executed against a separate clone of the old resolved
500-person fixture, **without running the new resolver**. Smoke verification:
250 active profiles, 2,819 actual gifts, 18 events, 24 snapshot months,
250 trait rows, all 21 traits matched, zero discrepancies including incremental
RFM. Full handler smoke time was 1.761s (not a 250k measurement). A separate
50-row incremental real-import smoke completed with 50 accepted, zero rejected,
exactly 50 pending records. Smoke evidence resides in
`.cache/m4-verification/followup-smoke/` and `incremental-smoke/`.

## Historical five-source M4 verification

The remaining sections preserve the earlier all-source verification, test runs,
and interrupted medium attempt. They are **historical evidence**, not claims
that those full suites or five-source timings were repeated for the new engine.
The commands remain usable; current resolver-first results and the authorized
250k fallback are reported above.

### Reproducible all-source runner

Run from `artifacts/audience-hub/backend` in an environment with the installed
backend dependencies and PostgreSQL binaries:

```sh
python -m app.benchmark --traits --profiles 1500 --output-dir /path/on/large-volume/m4
python -m app.benchmark --traits --profiles 500000 --output-dir /path/on/large-volume/m4
python -m app.benchmark --imports --test-suite --test-filter traits --output-dir /path/on/large-volume/tests
python -m app.benchmark --imports --test-suite --output-dir /path/on/large-volume/tests
```

Use an ordinary persistent terminal/job supervisor for the long medium run, not
a shell tool with a five-minute process-tree timeout. The runner refuses
`APP_ENV=production`, creates a new local `initdb` cluster, replaces application
credentials, disables TCP listening, and never uses the active `DATABASE_URL`.
Its private socket is under `/tmp/kinship-bench-*`; database files live under
the requested output directory. The private server is stopped and database
directory removed on normal completion or failure. CSVs and JSON evidence remain.
No active application database was modified during these checks.

The fixture uses the real `generate_seed(profiles, scale="medium",
random_seed=20250308)` and all five CSVs. The real loader performs mapping,
checksums, validation, normalization, error handling, and import submission.
The harness drains real `run_import` calls serially, then invokes production
identity resolution. This changes worker scheduling only; it is **not** an
import concurrency/throughput acceptance benchmark. Queued follow-up jobs are
cancelled only in this private database so the measured refresh runs once.

`traits.json` is checkpointed before/after expensive phases. Setup generation,
imports, and identity resolution are separate from the measured full trait job.
The current runner invokes `handlers.run("traits.recompute", {"mode":"full"})`,
pins the trait/snapshot functions to **2024-12-31**, and separately times
dashboard rollups and historical snapshot backfill. The full interval includes
lock acquisition, SQL upsert, first-month snapshot, dashboard rollups,
historical snapshots, dirty-row cleanup, commit, and cache invalidation.
Do not add nested timing values to the inclusive full-job duration.

To reuse an already generated medium fixture after an environment failure:

```sh
python -m app.benchmark --traits --profiles 500000 \
  --seed-dir /path/to/completed/seed \
  --output-dir /path/on/large-volume/retry
```

The reuse path validates requested population, density and random seed against
`generator_stats.json`; it still creates a fresh database and runs real imports.
Generated datasets are intentionally excluded from Git.

## Independent correctness evidence

The reference extends the raw-row approach of `scripts/traits_acceptance.py`.
It does not read SQL aggregates or trait values to construct expectations.
It streams all raw gifts, aggregates using Python and `Decimal`, calculates
global donor-only quintiles with ID tie-breaking, and independently aggregates
sample gifts, events and source appearances. Its expected key set must match
the registry. `computed_at` is checked against the actual full invocation's
wall-clock interval, not against the historical `as_of` date. Seventeen sampled
profiles are also incrementally recomputed and checked against global ranks.

Fresh completed run: `/tmp/m4-smoke/imports-ggyaheip/traits.json`.

| Item | Observed |
|---|---:|
| Requested people / density | 1,500 / medium |
| Resolved active profiles | 1,472 |
| Raw source records | 20,292 |
| Gifts / events | 16,000 / 542 |
| Unresolved records | 0 |
| Deterministic sampled profiles | **1,000** |
| Registry traits per profile | **21** |
| Reference discrepancies | **0** |
| Snapshot months | 24 |
| Generation / imports / identity | 1.457 / 6.312 / 16.790 seconds |
| Trait upsert SQL | 0.122 seconds |
| Dashboard rollups | 0.489 seconds |
| Recompute including current snapshot and rollups | 0.639 seconds |
| Historical backfill | 0.654 seconds |
| Actual full job including commit/cache invalidation | 1.475 seconds |
| Independent reference | 0.348 seconds |

Every import completed, with one intentionally unrecoverable generated row
rejected in each CSV. Requested people and resolved profiles differ because
the generator deliberately includes messy/shared identifiers; the report uses
actual database cardinalities rather than relabeling requested people.
These small-fixture timings are **not** medium-scale performance evidence.

## Focused and full tests

`tests/test_traits_verification_pg.py` exercises actual migrated PostgreSQL:

- donor ages 365/366/730/731 days, reactivation gaps 730/731 days;
- SQL quintiles compared to Python, including incremental global ranking;
- pinned historical dates, null/future gifts, 365-day giving,
  45-day recurring and 30-day video/event boundaries;
- all registry fields and actual computation timestamps;
- snapshot suppression while imports are queued or records unresolved;
- missing first-month snapshot escalating an incremental request to full;
- monthly snapshot immutability and idempotent 24-month backfill;
- actual full/dirty job handlers, old dirty-row cleanup and quiet-period retention;
- actual identity resolution marking its affected profile dirty.

Existing tests also verify the once-per-day nightly schedule.

Completed isolated focused run: **14 passed, 275 deselected** (7.46 seconds).
Completed isolated full suite: **288 passed, 1 skipped** (110.04 seconds).
The sole warning is the existing Starlette/AnyIO deprecation.
Evidence: `/tmp/m4-tests/imports-h2667plg/pytest.txt` and
`/tmp/m4-tests/imports-ddcyg47q/pytest.txt`.

A final whole-suite repeat produced **287 passed, 1 skipped, 1 failed**
(123.97 seconds): the existing
`test_reset_preserves_config_and_refuses_running_jobs` hit its busy-table refusal.
The failure was not in an M4 assertion. Repeating `-k 'traits or reset_demo'`
in another fresh disposable database passed **16 tests** (11.26 seconds).
No unrelated reset behavior was changed. Keep this intermittent whole-suite
failure visible rather than describing every run as green.
Evidence: `/tmp/m4-final-tests/imports-o7tnqsfl/pytest.txt` and
`/tmp/m4-final-tests/imports-nn82rw10/pytest.txt`.

## Medium run resource/checkpoint record

Environment: Python 3.12.12, PostgreSQL 17.5, 4 CPU quota, 8 GiB cgroup memory,
no swap. Initial `df` reported about 248 GiB free on the workspace volume and
32 GiB free under `/tmp`. The `/tmp` quota proved stricter than its `df` output.

The real 500,000-person medium generator completed in **369.211 seconds**:

| CSV | Generated rows |
|---|---:|
| donor_crm_contacts.csv | 504,952 |
| giving_platform_gifts.csv | 5,360,523 |
| esp_contacts.csv | 251,445 |
| five9_calls.csv | 178,605 |
| zeta_enrichment.csv | 500,002 |

The first complete-generation attempt failed during real gift staging:
PostgreSQL reported `Disk quota exceeded` and shut down. No OOM was recorded.
This attempt produced **no medium trait recompute timing**. Earlier
five-minute tool-timeout attempts likewise are not completed benchmark results.

The completed seed was retained at
`.cache/m4-verification/medium-seed` and reused for one workspace-volume retry:
`.cache/m4-verification/runs/imports-s_plmdfi`.
All five real imports completed in **2,808.417 seconds** (46.8 minutes):

| CSV | Accepted / rejected | Import duration |
|---|---:|---:|
| donor_crm_contacts.csv | 504,951 / 1 | 102.491 s |
| giving_platform_gifts.csv | 5,360,522 / 1 | 2,000.989 s |
| esp_contacts.csv | 251,444 / 1 | 288.116 s |
| five9_calls.csv | 178,604 / 1 | 182.448 s |
| zeta_enrichment.csv | 500,001 / 1 | 149.664 s |

Post-import database cardinalities: **6,789,124 source records, 5,360,521
gifts, 178,603 events**. These database counts differ from accepted CSV rows
because accepted duplicate rows are upserts rather than additional entities.
The database occupied 6,690,509,283 bytes at the recorded checkpoint.

Production identity started at 21:16:03 UTC. At 21:19:00 it had resolved 10,000
records; at 21:21:04 it had resolved 29,000. The observed 19,000-record progress
over approximately 124 seconds is about 153 records/second. Extrapolating that
short observation to the remaining 6.76M records implies roughly **12 hours**,
not a reliable completion ETA: contact/gift mixes, caches and later merges can
change the rate. Continuing unattended for hours was not justified for this
verification session.

At **2026-09-26 21:21:18 UTC**, the harness child was paused to record a stable
committed checkpoint: **31,000 resolved records, 30,962 active profiles, zero
profile trait rows**. It was then explicitly terminated. The parent runner was
joined; its `finally` stopped the private PostgreSQL server and removed its
database directory. Process inspection confirmed no remaining M4 PostgreSQL or
benchmark processes. The generated seed and import diagnostics are retained,
outside Git, for a longer-running machine/session. There was **no OOM**, and
the workspace retry had ample disk space; its blocker was setup runtime.

The medium attempt started with an earlier harness revision that directly
composed the same production recompute/backfill/cleanup functions. It never
reached those functions. The current reproducible runner invokes the actual
job handler and records dashboard timing separately, as demonstrated by the
completed 1,500-person run.

**Full five-source medium timing remains unmeasured.** To measure it, run the documented
500k command on a long-lived machine/session, allow real identity setup to
finish, then retain its full-job and reference results. Reusing the retained
seed avoids regeneration but does not avoid the real imports/identity work.
The new 250k typed-COPY fallback above is explicitly a different,
user-authorized scope; it is not relabeled as this missing full-medium
measurement. No partially resolved population, dashboard-only interval, or
small-run extrapolation is presented as a full-medium result.