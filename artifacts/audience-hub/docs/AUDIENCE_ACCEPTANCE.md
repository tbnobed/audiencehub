# Step 5 audience acceptance

The editable catalog contains the 23 audiences in `ZETA_AUDIENCE_TARGETS.json`.
Source attributes, not computed donor/RFM traits or artificial cohort labels,
drive the audited rules. Untouched, never-materialized bootstrap drafts are
repurposed for the new catalog. Edited, active, owned, or referenced legacy
audiences retain their saved definitions, memberships and history as ordinary
audiences.

`exists` rejects null, empty and whitespace-only values. Boolean false is a
present value. Activity and donation lookbacks cover complete UTC days
`[as_of - N days, as_of)`. Living Legacy uses the actual five-calendar-year
anniversary (February 29 clamps to February 28). Giving Tuesday is fixed to
December 3, 2024 UTC and the four specified source codes. VOD membership
requires exactly the `vod_viewed` event name.

## Isolated acceptance runs

Run 50,000 before starting the 500,000 background run:

```
kinship benchmark --audit-segments --profiles 50000 --scale small
kinship benchmark --audit-segments --profiles 500000 --scale medium
```

The existing restricted-schema backend is supported via
`KINSHIP_BENCHMARK_BACKEND=schema`; otherwise the existing private PostgreSQL
socket runner is used. `--audit-segments` is an explicit fresh isolated exception
to the read-only benchmark default. Plain `--segments` now measures all 23
code-defined templates against existing app data, skips materialization and
never installs or changes saved audiences. `--segments --isolated` retains the
six legacy synthetic benchmark definitions. `--as-of YYYY-MM-DD`
pins the reference date; its default is today UTC. `--seed-dir` reuses only
matching audit CSV files, never application data. The outer runner drops the
private database/schema and generated data at completion. For existing-data
benchmark flag, timing and safety details, see [Segments](SEGMENTS.md#benchmark-modes).

`app.segments.audit_verification.benchmark_loaded(engine, directory, as_of=...)`
reuses an already loaded **isolated** database. It refuses application databases.
`measure` first performs real imports, identity resolution, traits, rollups and
Step 4 independent acceptance.

Each run writes `audit-seed.json` and `audience-acceptance.json` in its evidence
directory. The audience report checkpoints all 23 definitions, independent SQL,
exact identity-set differences, sample membership, committed materialization
identity parity, cold/warm count and preview times, sample time and materialize
time. “Cold” means first operation in a fresh session, not flushed OS/PG caches.
Expected population is `N * real_count / 7807899`; tolerance is
`max(0.20 * expected, 25)`. A count-only match cannot pass identity parity.

Tests: `test_audit_segments.py`, `test_audit_segments_pg.py`, and the updated
segment API/compiler tests. Full population tests are opt-in with
`KINSHIP_AUDIT_SEGMENTS_PROFILES=50000` or `500000` inside the private runner.
No population run or performance result is claimed by this implementation
document; retain the actual generated JSON reports here after acceptance runs.

## Bulk-phase timeout correction

The 500k attempt reported by the operator completed its five imports but failed
in identity resolution. It is **NOT PASSED**; no 500k audience timing is inferred.
Direct `handlers.run` in the isolated harness does not enter the worker lifecycle:
the actual PostgreSQL connection inherits the API 60-second timeout rather than
the worker identity/traits 7,200-second budget.

The harness now attaches a private-engine `begin` listener separately around
identity, traits and rollups. Each transaction receives a transaction-local bulk
budget, including transactions after commit/rollback. The listener is removed
in `finally`; production connection defaults are unchanged. Imports retain their
own existing bulk-budget lifecycle. The segment preview guard remains 10 seconds.

`phase-timings.json` checkpoints each completed phase and, on exceptions, keeps
the failing phase, elapsed seconds, error and all completed timings. These are
real measured durations, not estimates. Small isolated PostgreSQL tests in
`test_audit_phase_budget.py` prove the 60s default, 7200s phase budget across
commit/rollback, restoration after success/failure, listener removal and the
unchanged 10s preview guard. No 500k rerun was launched for this correction.

## Measured results

50,000 profiles: all 23 identity sets, counts, samples and materializations passed. Reference date 2026-10-17 UTC. Full evidence: AUDIENCE_ACCEPTANCE_50K.json. Cold means application first execution, not a cold OS/PostgreSQL cache. Warm columns below are medians of three repetitions.

| Audience | Count | Count first ms | Count warm ms | Preview first ms | Preview warm ms | Sample ms | Materialize ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| All TBN Reference | 50000 | 351.5 | 17.2 | 16.3 | 16.6 | 89.8 | 1375.8 |
| Has Active Email | 28659 | 562.9 | 427.4 | 424.9 | 396.2 | 1033.8 | 956.0 |
| Active (120 days) | 18585 | 1108.3 | 725.8 | 774.9 | 787.4 | 1649.4 | 1341.6 |
| TBN Plus Subscription | 13364 | 435.7 | 357.5 | 343.9 | 354.5 | 917.0 | 805.9 |
| TBN Plus Free Users | 13131 | 885.4 | 652.9 | 390.0 | 395.9 | 895.7 | 1710.1 |
| TBN Plus Premium | 233 | 2650.5 | 868.7 | 1107.1 | 741.4 | 1640.7 | 469.7 |
| Premium Upgraders | 56 | 500.1 | 396.7 | 480.4 | 371.2 | 1142.8 | 420.2 |
| TBN App Non-Donors | 12295 | 487.6 | 696.7 | 1036.1 | 972.8 | 1137.0 | 1408.1 |
| TBN App Donors | 108 | 33.9 | 23.8 | 17.3 | 15.1 | 118.3 | 156.6 |
| Basic Donors | 462 | 1039.8 | 701.4 | 993.0 | 696.0 | 954.3 | 483.3 |
| Basic Recurring Donors | 30 | 728.1 | 445.3 | 475.8 | 428.6 | 1171.0 | 472.9 |
| Basic One-Time Donors | 72 | 442.7 | 442.4 | 430.8 | 420.8 | 1089.0 | 470.2 |
| RFM TBN All Donor Reference | 11268 | 533.6 | 511.4 | 519.6 | 496.3 | 1067.9 | 1051.6 |
| RFM Lapsed Donors | 10299 | 453.1 | 363.3 | 406.8 | 378.9 | 808.1 | 937.2 |
| Partner Names Non-Donors | 3827 | 503.0 | 496.9 | 507.5 | 495.5 | 1090.8 | 730.7 |
| Donors Past 30 Days | 121 | 14.1 | 11.2 | 10.3 | 10.2 | 35.9 | 27.9 |
| Living Legacy Prospects V2 | 5 | 212.1 | 156.2 | 190.0 | 149.5 | 396.0 | 258.8 |
| Giving Tuesday 2024 | 5 | 22.2 | 13.9 | 14.7 | 13.9 | 44.6 | 33.7 |
| Mobile Subscribed | 571 | 68.4 | 51.2 | 60.5 | 53.5 | 119.3 | 198.4 |
| Call Center SMS Opt-ins | 958 | 76.5 | 73.9 | 71.4 | 56.3 | 120.4 | 202.0 |
| SMS Opt-Out Date Exists | 252 | 470.5 | 331.0 | 377.9 | 363.7 | 742.6 | 387.0 |
| VOD Optouts | 11654 | 457.5 | 437.2 | 466.3 | 483.1 | 967.1 | 789.5 |
| Customers with VOD Viewed | 9056 | 69.5 | 26.3 | 23.3 | 24.4 | 107.3 | 186.1 |

### 500k acceptance: NOT PASSED

The final attempted run completed all five imports (500,000 CRM contacts, 118,526 gifts, 286,585 ESP contacts, 765,800 events, 245,000 enrichment source rows), then hit a statement timeout while writing identity ownership, identifiers and crosswalks. The direct benchmark handler call lacked the worker bulk-timeout context. It did not reach the audience measurements; no 500k audience counts or latency figures are claimed.

Earlier attempts found a duplicate generated CSV header and a migration JSON binding error; both were corrected. The importer timeout fix allowed all imports to complete. The remaining harness phase-budget fix is verified separately, but another long 500k attempt has not been run. Failure cleanup removed the isolated database and generated data. The failed run ran alongside regression checks and initially encountered overly broad development watchers, so its wall time is not a clean production projection.
