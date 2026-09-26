# M4 verification

**Result: correctness verified; medium nightly timing remains unverified.**
The medium seed was generated and all five real imports completed, but production
identity setup was stopped after its measured progress demonstrated a many-hour
remaining setup estimate. No medium recompute duration or under-ten-minute pass
is claimed. This is a concrete remaining M4 acceptance gap, not M4 completion.
Compact machine-readable evidence: `m4-verification-results.json`.

## Reproducible runner

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

**Medium timing acceptance is incomplete.** To close it, run the documented
500k command on a long-lived machine/session, allow real identity setup to
finish, then retain its full-job and reference results. Reusing the retained
seed avoids regeneration but does not avoid the real imports/identity work.
No thin SQL fixture, partially resolved population, dashboard-only timing, or
small-run extrapolation was substituted for the missing medium measurement.