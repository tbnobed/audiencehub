# Import performance evidence

## Reproduce safely

Run as a **non-root user** on a development/benchmark host with Python dependencies
installed and PostgreSQL 17 `initdb`, `pg_ctl`, and `createdb` on PATH:

```sh
cd artifacts/audience-hub/backend
python -m app.cli benchmark --imports
# Isolated 100k gift profile (no contact workload):
python -m app.cli benchmark --imports --gift-rows 100000 --contact-rows 0 --profile
# Entire backend test suite, including real bulk SQL and worker recovery:
python -m app.cli benchmark --imports --test-suite
```

The production slim Docker image intentionally does **not** include PostgreSQL
server tools. Do not run this command inside that image; use the host command
above with the same application checkout/dependencies. This avoids inflating the
production image or granting its application user database-creation privileges.

Each invocation initializes a private disposable PostgreSQL cluster, database and
role named `benchmark`, using a private Unix socket and **no TCP listener**.
It never connects to the configured application database. It generates its own
test-only settings and cryptographic keys and refuses `APP_ENV=production`.
No existing credentials are read for benchmark configuration. Cluster data is
removed on normal exit; generated inputs/results/profiles remain in
`.cache/kinship-benchmarks/imports-*` (ignored by git). Override `--output-dir`
with an ignored workspace path for retained evidence. Interrupted/killed runs can
leave cluster directories; do not treat those as completed measurements.

Default inputs are deterministic, synthetic 1,000,000 gifts and 500,000 contacts,
unique external IDs/emails, ISO gift dates, $25.50 amounts, and a repeated valid
synthetic telephone number on contacts. This is a clean-data throughput workload,
not a representative distribution of malformed production inputs or identity
resolution costs.

Both imports run concurrently in separate processes with independent database
sessions using the real `run_import` entry point (the initial harness used
threads; its separate results are explicitly labeled below). Timings include validation,
normalization and database writes; exclude CSV generation, cluster/migration
setup, queue wait, identity resolution and trait calculation. No background
identity/trait worker competes for resources. Throughput is per-import rows divided
by its complete wall time. Acceptance additionally checks committed source-record
and gift-table counts, completed status and zero rejected rows. Exit status is
nonzero if any throughput/count/time acceptance check fails.

## Environment

Replit Linux workspace; Python 3.12.12; PostgreSQL 17.5; SQLAlchemy 2.0.40;
psycopg 3.2.6. Cgroup `cpu.max` was `400000 100000`: **4 CPU quota**.
`os.cpu_count()` can expose more logical CPUs and must not be confused with that
quota. PostgreSQL uses default local-cluster settings, with durable commits (no
benchmark-specific disabled fsync). Other workspace processes may share this quota.

## 100,000-gift cProfile comparison

Both measurements enable cProfile; these are **not unprofiled acceptance runs**.
BEFORE uses the pre-change snapshot `/tmp/kinship-before-xrd5be` on PYTHONPATH.
AFTER uses the updated source. Each gets a fresh, migrated database.

| Measurement | Wall seconds | Rows/second | Accepted / rejected |
|---|---:|---:|---:|
| BEFORE | 95.755 | 1,044.33 | 100,000 / 0 |
| AFTER (initial optimized revision) | 37.546 | 2,663.42 | 100,000 / 0 |
| AFTER (final service revision) | 41.927 | 2,385.10 | 100,000 / 0 |

Observed profiled speedup: **2.55×**. AFTER additionally verified 100,000 source
records and 100,000 gifts. BEFORE preceded the persisted-count addition to the
runner and only checked import counters. This is not evidence of meeting 5,000
rows/s or the full concurrent workload acceptance threshold.

Retained raw evidence (workspace-relative):

* `.cache/kinship-benchmarks/before/imports-qhgfasc5/`
* `.cache/kinship-benchmarks/after/imports-n8jw66gl/`
* `.cache/kinship-benchmarks/final-profile/imports-y33elxbv/`

Each contains `gift.prof`, `gift-top20.txt`, `gift.csv`, and `results.json`.
The profiler counts below use cumulative time; parent/child costs overlap.

### BEFORE cumulative top 20

130,935,387 calls (130,929,214 primitive), 94.262 profiler seconds.

| Calls | Self seconds | Cumulative seconds | Function |
|---:|---:|---:|---|
| 200000 | 4.618 | 68.078 | validation.py:141 map_and_validate_row |
| 200000 | 1.647 | 51.118 | validation.py:22 normalize_email |
| 200000 | 1.838 | 47.590 | validate_email.py:15 validate_email |
| 200000 | 4.677 | 38.745 | syntax.py:446 validate_email_domain_name |
| 800000 | 2.104 | 21.812 | core.py:558 ulabel |
| 800000 | 5.001 | 18.294 | core.py:442 check_label |
| 1807 | 0.026 | 15.520 | session.py:2138 _execute_internal |
| 1805 | 0.004 | 15.486 | session.py:2305 execute |
| 1807 | 0.005 | 15.449 | base.py:1372 execute |
| 1807 | 0.005 | 15.443 | elements.py:514 _execute_on_connection |
| 1807 | 0.024 | 15.438 | base.py:1588 _execute_clauseelement |
| 1807 | 0.014 | 15.259 | base.py:1788 _execute_context |
| 1807 | 0.016 | 13.254 | base.py:1847 _exec_single_context |
| 3204 | 6.124 | 13.233 | connection.py:399 wait |
| 200000 | 1.011 | 12.935 | core.py:799 decode |
| 200000 | 0.867 | 12.892 | core.py:732 encode |
| 400000 | 0.533 | 11.413 | core.py:524 alabel |
| 400 | 0.001 | 10.451 | default.py:941 do_executemany |
| 400 | 0.007 | 10.450 | cursor.py:100 executemany |
| 200 | 0.498 | 9.203 | service.py:319 _upsert_source_records |

### AFTER cumulative top 20 (initial optimized revision)

39,931,989 calls (39,926,134 primitive), 36.170 profiler seconds.

| Calls | Self seconds | Cumulative seconds | Function |
|---:|---:|---:|---|
| 3010 | 9.777 | 11.659 | connection.py:399 wait |
| 117 | 0.002 | 11.291 | session.py:2138 _execute_internal |
| 117 | 0.001 | 11.284 | base.py:1372 execute |
| 117 | 0.000 | 11.283 | elements.py:514 _execute_on_connection |
| 117 | 0.002 | 11.282 | base.py:1588 _execute_clauseelement |
| 120 | 0.001 | 11.259 | cursor.py:80 execute |
| 115 | 0.000 | 11.253 | session.py:2305 execute |
| 117 | 0.001 | 11.233 | base.py:1788 _execute_context |
| 117 | 0.001 | 11.224 | base.py:1847 _exec_single_context |
| 117 | 0.000 | 11.213 | default.py:944 do_execute |
| 30 | 0.507 | 9.745 | staging.py:31 copy_upsert |
| 100000 | 1.939 | 9.533 | validation.py:137 map_and_validate_row |
| 10 | 0.478 | 7.892 | service.py:339 _upsert_source_records |
| 10 | 0.339 | 5.059 | service.py:383 _insert_gifts |
| 500013 | 0.813 | 4.112 | __init__.py:183 dumps |
| 10 | 0.004 | 3.383 | result.py:2063 all |
| 10 | 0.138 | 3.379 | result.py:543 _allrows |
| 13 | 0.000 | 3.153 | cursor.py:2134 _fetchall_impl |
| 13 | 0.000 | 3.153 | cursor.py:1131 fetchall |
| 10 | 0.000 | 3.153 | result.py:1673 _fetchall_impl |

Validation calls fell from twice per row to once, and SQL execution calls from
1,807 to 117. Database wait/result processing is now a larger fraction. These
observations support the changes but do not establish scale acceptance.

### Final service revision cumulative top 20

33,722,000 calls (33,716,154 primitive), 40.736 profiler seconds. Despite fewer
calls than the initial revision, this run had more database wait time and was
slower: **2.28× faster than BEFORE**, not 2.55×. No preceding benchmark was running.
This isolated single-import profile still used the original one-thread executor;
that does not introduce cross-import GIL contention.

| Calls | Self seconds | Cumulative seconds | Function |
|---:|---:|---:|---|
| 2554 | 18.701 | 19.639 | connection.py:399 wait |
| 97 | 0.002 | 18.900 | session.py:2138 _execute_internal |
| 97 | 0.000 | 18.893 | base.py:1372 execute |
| 97 | 0.000 | 18.892 | elements.py:514 _execute_on_connection |
| 97 | 0.002 | 18.892 | base.py:1588 _execute_clauseelement |
| 100 | 0.001 | 18.865 | cursor.py:80 execute |
| 95 | 0.000 | 18.864 | session.py:2305 execute |
| 97 | 0.001 | 18.845 | base.py:1788 _execute_context |
| 97 | 0.001 | 18.838 | base.py:1847 _exec_single_context |
| 97 | 0.000 | 18.829 | default.py:944 do_execute |
| 30 | 0.507 | 18.702 | staging.py:31 copy_upsert |
| 10 | 0.500 | 10.946 | service.py:339 _upsert_source_records |
| 10 | 0.323 | 10.242 | service.py:396 _insert_gifts |
| 100000 | 1.910 | 9.237 | validation.py:137 map_and_validate_row |
| 400013 | 0.641 | 3.342 | __init__.py:183 dumps |
| 100096 | 0.186 | 3.174 | builtins.next |
| 40 | 0.001 | 2.951 | staging.py:14 staging_table |
| 400013 | 0.521 | 2.560 | encoder.py:183 encode |
| 100000 | 0.515 | 2.479 | validation.py:22 normalize_email |
| 10 | 0.460 | 2.462 | service.py:191 _identifiers_blocked_batch |

## Initial full concurrent scale run (threads)

Completed, unprofiled, 4 CPU quota; the original runner used two threads and
therefore shared Python's GIL. Evidence:
`.cache/kinship-benchmarks/scale/imports-btcitu0j/results.json`.

| Import | Rows | Seconds | Rows/s | Stored source rows | Stored gifts |
|---|---:|---:|---:|---:|---:|
| Gift | 1,000,000 | 299.921 | 3,334.21 | 1,000,000 | 1,000,000 |
| Contact | 500,000 | 198.261 | 2,521.93 | 500,000 | — |

Both completed with zero rejects. Gift `<300s` passed narrowly; **both ≥5,000
rows/s checks failed**. The final runner uses independent processes to match
production worker isolation instead of constraining both imports to one GIL.

## Final full concurrent scale run (processes)

Final service revision, unprofiled, 4 CPU quota, two independent import processes.
Started only after the preceding full run and final profile had finished; tests
were run afterward, not during this measurement. Evidence:
`.cache/kinship-benchmarks/final-scale/imports-7gjpx39g/results.json`.

| Import | Rows | Seconds | Rows/s | Stored source rows | Stored gifts |
|---|---:|---:|---:|---:|---:|
| Gift | 1,000,000 | 216.780 | 4,612.98 | 1,000,000 | 1,000,000 |
| Contact | 500,000 | 113.170 | 4,418.13 | 500,000 | — |

**Overall acceptance: FAIL.** Both imports completed with all requested rows and
zero rejects; all persisted-count checks passed. Gift `<300s` **PASS**.
Gift `≥5,000 rows/s` **FAIL**; contact `≥5,000 rows/s` **FAIL**.
The measured improvement is real but the requested per-import throughput target
has not been achieved on this host. No extrapolation, unmeasured tuning, or
profiling-adjusted estimate is substituted for measured acceptance.

## PostgreSQL correctness

`benchmark --imports --test-suite` on the isolated migrated PostgreSQL cluster:
**122 passed, 1 skipped**, 12.25 seconds (one unrelated deprecation warning).
Evidence: `.cache/kinship-benchmarks/tests/imports-nmff1xc0/pytest.txt`.
The added real-SQL smoke exercises insert and conflict paths for source records
(including numbered column names), gifts, events, enrichment metadata, all five
enrichment value types, and consent including equal-timestamp opt-out precedence.
It asserts table counts and stored values, and rolls back. Worker subprocess
recovery is enabled against that same disposable database.

Final service/worker revision rerun: **122 passed, 1 skipped**, 12.95 seconds,
including the same real SQL all-target smoke and worker crash/recovery test.
Evidence: `.cache/kinship-benchmarks/final-tests/imports-rzlticd3/pytest.txt`.
Python compilation of the benchmark, CLI and added tests also passed.