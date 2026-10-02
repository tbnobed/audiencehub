# Reset-test isolation and verification

## Root cause

The intermittent reset failure was reproduced in a fresh disposable PostgreSQL
full-suite run. After the NOWAIT failure, a diagnostic join of `pg_locks`,
`pg_stat_activity`, and `pg_class` identified an **autovacuum worker** running
`VACUUM public.source_records`, holding `ShareUpdateExclusiveLock` on that table.
Earlier large-fixture tests had caused enough table churn to trigger maintenance.
The reset's requested `AccessExclusiveLock` correctly conflicted with it.

The diagnostic run `imports-0vbawv_g` recorded PostgreSQL error `55P03`,
backend PID 2709, backend type `autovacuum worker`, state `active`, relation
`source_records`, lock `ShareUpdateExclusiveLock`, and query
`autovacuum: VACUUM public.source_records`. This was not inferred from a later
successful retry. An earlier attempt hit the already-known storage quota before
reaching the reset test and is not correctness evidence. Raw disposable logs
were removed after this portable evidence summary was recorded.

## Fix

`backend/tests/test_reset_demo.py` now gives each reset integration test its own
new, migrated database within the benchmark-owned disposable PostgreSQL cluster.
These tiny fixtures have no prior scale-test churn or unrelated sessions.
The fixture disposes its connections and drops its database on exit.

Production reset code is unchanged: NOWAIT, transaction atomicity, running-job
checks, and the table allowlist remain intact. There are no retries, sleeps,
maintenance cancellations, or autovacuum configuration changes.

A new regression test deliberately holds `ShareUpdateExclusiveLock` on
`source_records` from a second connection, verifies that reset refuses and
preserves existing profile rows, then releases the lock and verifies that reset
succeeds. This exercises the observed conflict deterministically.

## Final five consecutive full-suite runs

These final runs cover the stable pre-M5 code including the incremental-only
identity write optimization and the isolated-schema production benchmark
hardening. They supersede an earlier five-pass sequence with 311 passing tests.

Command, from `artifacts/audience-hub/backend`, with a different output directory
for each run:

```sh
python -m app.benchmark --imports --test-suite \
  --output-dir /path/to/disposable-evidence/final-runN
```

| Run | Run identifier | Result | Duration |
|---|---|---|---:|
| 1 | `imports-5tvujx5h` | 324 passed, 1 skipped | 179.86 s |
| 2 | `imports-w7zo36zb` | 324 passed, 1 skipped | 158.74 s |
| 3 | `imports-vz2gimqj` | 324 passed, 1 skipped | 171.44 s |
| 4 | `imports-avl15bq1` | 324 passed, 1 skipped | 173.77 s |
| 5 | `imports-nd1cjh8n` | 324 passed, 1 skipped | 156.70 s |

All five exits were zero, consecutively, without retrying any failed final run.
Each log contained one existing Starlette/AnyIO
deprecation warning. Each run used a fresh private cluster; the harness stopped
and removed its database files afterward. After verification, all reset-owned
diagnostic logs, final-run logs, disposable files and retained pytest temporary
directories were deleted at the user's request. No private environment files
are retained. This checked-in summary is the portable evidence.
The active application and production databases were never used.

SHA-256 digests of the five original pytest logs, in run order:

```text
ee9956a454585ceba3808a9af8dc644e755433c76ebf5ba5eb336f9f447490d6
208cb2c3ebf5cdac9c73d7fd986d2efd4ea4a883067ff368475bfee5899471d6
cede7e89dd898ec02429feab13930f5008333cc6cf760d768c361d5cadc93c86
41381067e02f85a3276d0a22bd1408c4d741e57e6c323c19e99e86e5a7fa3487
4d0880fccde7e3c50a16cd466d2d0461e939911aa06bcf3bb4cdf6a3f164d20e
```

Final verified source SHA-256:

| File under `backend/` | SHA-256 |
|---|---|
| `tests/test_reset_demo.py` | `5a730b4ce7061c88924a51134b1235c410f70660e730ef9c1056b47910da384b` |
| `app/identity/bulk.py` | `737644de10af6de430a98b87a8be3df8b6c956576bef977467c08575b7556303` |
| `app/schema_benchmark.py` | `2e9ab4ed9afd2085ad907a5cc9cd1ba78ed5591f50c8bfd2f25730d2f008f4e3` |

These results cover the working tree exercised by these runs, before M5.
They are not a claim about subsequently added code or performance benchmarks.