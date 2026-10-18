# M5 verification

## Pre-M7 segment benchmark and small-seed plausibility

The production-container CLI now supports:

```sh
docker compose exec api kinship benchmark --segments --isolated --profiles 500000 --scale medium
```

Rebuild the application image/update Compose before using the new flag. As with
`--traits`, container mode provisions a new restricted login and isolated schema
in the existing Postgres container. Workload connections cannot read or modify
live application tables. Schema/role and generated seed/upload/export files are
removed on normal completion or handled interruption; compact `segments.json`
and cleanup receipts remain under `/data/benchmarks/segments-*`. Existing
privilege prerequisites and host-loss/SIGKILL limitations in `M4_VERIFICATION.md`
still apply. This shares server resources; it never flushes shared database/OS
caches or restarts production Postgres.

Each template prints its count, cold preview milliseconds, median of three warm
previews, sample milliseconds, materialization milliseconds, and plausibility
PASS/FAIL. Cold means the first preview in a newly connected pool **after fixture
preparation**, not cold PostgreSQL buffers or cold OS storage. Warm runs reuse the
same session and pinned `as_of`. Preview includes validation, compilation, count
and the API's 10-second timeout guard; sample includes the 25-row sample and
breakdowns; materialization includes membership, history, audit and commit.
Generation, real CSV imports, resolution and traits are timed separately.
`--scale small|medium|large` controls seed density; `--profiles` controls requested
people. Local disposable-cluster mode also supports the flag.

### Actual small-seed report

Executed against a restricted schema inside a disposable PostgreSQL cluster:

```sh
kinship benchmark --segments --isolated --profiles 50000 --scale small
```

Seed: **50,000 requested people**, scale **small**, random seed **20250308**,
pinned **as_of 2024-12-31 UTC**. All five CSVs went through the real importer and
resolver. Result: **49,151 active partners**, **480,225 gifts**, **3,731 events**,
**608,956 source records**, no unresolved records.

Each expected range below is derived by its own handwritten SQL query, not by
the segment compiler. The queries independently inspect `profile_traits` and raw
`gifts` (plus `events`/`consents` where required). A fixed population and pinned
date yield an exact closed interval **[expected, expected]**, rather than an
arbitrary percentage tolerance. The top-5% query independently ranks raw positive
365-day giving, rounds population × 5% upward, and includes boundary ties.
Queries: [`segment-small-plausibility.sql`](segment-small-plausibility.sql).
Complete evidence, raw timings, SQL and full file hashes:
[`segment-small-plausibility.json`](segment-small-plausibility.json).

| Partner template | Actual count | Independently expected range | Result |
|---|---:|---:|---|
| Lapsed, lifetime giving ≥ $500 | 39 | [39, 39] | PASS |
| First gift in last 90 days | 843 | [843, 843] | PASS |
| Active recurring | 7,420 | [7,420, 7,420] | PASS |
| Top 5% by 12-month giving, ties included | 620 | [620, 620] | PASS |
| 3+ video views in 30 days, never given | 0 | [0, 0] | PASS |
| Email opted-in, no gift in 365 days, 2+ earlier gifts | 1,404 | [1,404, 1,404] | PASS |

Zero for the video template is expected: the small seed emits Five9 call events,
not `Video Watched` events. No additional events or fabricated matches were added
to make that template nonzero.

| Template key | Cold preview ms | Warm median ms | Sample ms | Materialize ms |
|---|---:|---:|---:|---:|
| lapsed-high-value | 67.90 | 29.30 | 99.04 | 65.94 |
| new-partners | 33.89 | 22.70 | 67.34 | 102.08 |
| recurring-partners | 47.84 | 23.99 | 76.63 | 248.12 |
| top-giving | 66.43 | 40.00 | 124.59 | 75.10 |
| engaged-not-giving | 52.47 | 25.69 | 85.88 | 45.21 |
| lapsing-opted-in | 99.65 | 74.47 | 177.48 | 120.19 |

SHA-256 prefixes (full hashes in JSON):

| Seed CSV | SHA-256 prefix |
|---|---|
| donor_crm_contacts.csv | `7579cb835009e4ea` |
| giving_platform_gifts.csv | `d8c9647d593eff08` |
| esp_contacts.csv | `69d6688a407aa5b1` |
| five9_calls.csv | `4609004ea199df0a` |
| zeta_enrichment.csv | `47cbb3f46d79721f` |

All six materialized counts matched both previews and independent references.
The production-configured CLI also passed a separate 1,500-person schema-isolation
smoke test; a sentinel public table and public job count remained unchanged, and
the owned schema/role were removed. Failure and interruption cleanup are tested
for both trait and segment modes. The final focused verification passed
**6 tests** (385 deselected); the separate actual 50,000-person acceptance test
passed in 593.86 seconds including all fixture preparation. Disposable fixtures/clusters were removed after
retaining these portable reports.

**This is small-scale plausibility, not production-scale acceptance.** No
500k-person sandbox benchmark was run; medium-scale timing remains pending the
user's production-server run.

## Delivered

Partner audience list, tree builder and detail pages are connected to the segment
API. All DSL node families, typed operators, pinned windows, validation paths,
role-sensitive samples/JSON, dependency-aware materialization, scheduled refresh,
history, audit-logged CSV export and six editable templates are implemented.
Activation-time materialization is exposed as a helper for the later activation
milestone; activation execution itself is not implemented by M5.

## Evidence

- Final backend suite before the history-export addition: **385 passed, 1 skipped**.
- The subsequent history-export addition passed its focused PostgreSQL test,
  including viewer denial, exact CSV output and persisted audit entry.
- Frontend: **9 logic tests, 12 render/interaction tests** passed; existing profile
  tests remained **125/125**. Typecheck and production build passed.
- Signed-in browser lifecycle passed at 1280×900 and 1024×768: nested rule editing,
  duplicate/reorder/delete, live preview and per-rule impact, draft persistence,
  invalid JSON node-path feedback, active save, queued refresh completion, count
  history, table/CSV export and audit, duplication and archive confirmation.
- Unsaved edits survived the 10-second detail polling interval.
- The browser test archived only its own two audiences. All six seeded templates
  remained unchanged. Role restrictions also have backend and frontend coverage;
  the browser lifecycle was performed as admin, not as a separate viewer journey.

The browser's selected definition matched zero partners; preview, sample, members
and materialized count consistently agreed. Its 6.2 ms preview is a smoke check,
**not a medium-scale performance result**. Medium-scale preview timing remains
unmeasured; no 500k sandbox benchmark was run.

## Operational notes

See `SEGMENTS.md` for scheduling, trait-success barriers, reference/cycle rules,
delta pruning and controlled migration instructions. M4's separate five-pass
pre-M5 gate and production benchmark command are in `M4_VERIFICATION.md`.
Disposable benchmark/test clusters and generated fixtures were removed; portable
verification reports remain in this documentation directory.