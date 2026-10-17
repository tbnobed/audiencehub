# Relative-date seed verification

`kinship seed --as-of YYYY-MM-DD` pins the reference date. Omitting it captures
today in UTC once. All generated timestamps are within the five-calendar-year
anniversary through yesterday, inclusive. Leap-day anniversaries clamp to
February 28. Metadata records `as_of`, `start_date`, and `end_date`.

Example (generation only, no database access):

```sh
kinship seed --profiles 50000 --scale small --as-of 2026-10-17 --output-dir /tmp/relative-seed
```

Traits, RFM, historical snapshot cutoffs and current-status rollup publication
use the explicit reference date, or UTC today. Event day bucketing is UTC.
Neither the generator nor these calculations uses the maximum gift date as
the reference date. The CLI passes its reference into final trait computation
and snapshot backfill. API-generated seed messages are timestamped yesterday;
normal ingestion timestamp limits still apply to explicitly old dates.

Unset dashboard defaults now resolve to rolling 12 months in UTC. Saved custom
and 90-day selections remain untouched. Migration `0029` permits storing the
12-month preset without rewriting settings.

## Measurement scope

The isolated 50,000-person fixture uses UTC **2026-10-17**, with history
**2021-10-17 through 2026-10-16**. The generator preserves deliberate invalid
sentinels and duplicates for importer tests; valid timestamps are bounded.
Imported identity profiles can differ slightly from person-level generator
cohorts because intentional households and matching rules remain in place.

Campaign and annual tables below measure unique valid generated gifts by
contact external ID, not identity-resolved profiles. The status/template checks
use the actual isolated imported database.

### Campaign dollar shares

| Campaign | Share |
| --- | ---: |
| Year-End Giving | 31.486% |
| Monthly Partner Program | 20.128% |
| Spring Outreach | 11.290% |
| Global Missions | 8.871% |
| Broadcast Ministry | 7.258% |
| Community Relief | 5.645% |
| Easter Hope | 4.032% |
| Summer Youth | 3.226% |
| Bible Translation | 2.823% |
| Food for Families | 2.016% |
| Prayer Outreach | 1.613% |
| Legacy Impact | 1.613% |

Median gift: **$52.41**. Gifts at least $1,000: **1.835%**.
Gifts at least $10,000: **72**.

### Calendar-year seasonal share and retention

| Year | Coverage | One-time Nov–Dec share | Retention from preceding calendar year | Dollar growth |
| --- | --- | ---: | ---: | ---: |
| 2021 | Partial, Oct 17 onward | 76.932% | Not available | Not comparable |
| 2022 | Complete, prior year partial | 30.000% | 86.089% (partial prior cohort) | Not comparable |
| 2023 | Complete | 30.000% | 49.719% | 4.500% |
| 2024 | Complete | 30.000% | 50.020% | 4.500% |
| 2025 | Complete | 30.000% | 42.852% | 4.500% |
| 2026 | Partial, through Oct 16 | 0% (Nov–Dec not yet present) | 46.505% (year-to-date) | Not comparable |

Do not force future November/December gifts into a partial current year to meet
an annual seasonality target. The 30% and annual growth targets apply to complete
calendar years; the first and last years are explicitly partial.

## Reproduction

From `artifacts/audience-hub/backend`:

```sh
APP_ENV=test KINSHIP_BENCHMARK_BACKEND=local python -m app.benchmark \
  --segments --profiles 50000 --scale small \
  --output-dir .cache/relative-seed-acceptance
```

The benchmark creates and removes its own PostgreSQL instance; it must never
target the live database. `segments.json` contains independently queried
template expectations, measured counts and status/dashboard acceptance.
`python -m app.seed_report <seed-directory>/giving_platform_gifts.csv` reproduces
the independent campaign and annual CSV report.

## Imported small-seed result: PASS

The real five-CSV importer, identity resolver, attachment, trait computation and
rollups produced 49,268 active profiles, 186,553 gifts, 18,908 events, and 34,524
givers. No live data was read or modified.

| Giver group | Count | Share |
| --- | ---: | ---: |
| Active, including new/reactivated | 13,917 | 40.311% |
| Lapsing | 4,144 | 12.003% |
| Lapsed | 16,463 | 47.686% |

The active group includes 3,593 ordinary active, 5,142 new (existing trait
definition: first gift within 365 days), and 5,182 reactivated profiles.
The narrower new-partner template below measures 90 days. There are 1,224
currently recurring profiles. Current rollup publication matches trait counts
and the explicit reference date.

The last-12-month dashboard contains **38,645 gifts totaling $5,479,163.51**.

| Template | Imported count | Independent SQL / materialization |
| --- | ---: | --- |
| Lapsed high-value | 3,504 | PASS |
| New partners (90 days) | 324 | PASS |
| Recurring partners | 1,224 | PASS |
| Top giving | 695 | PASS |
| Engaged, not giving | 4,883 | PASS |
| Lapsing, opted in | 7,179 | PASS |

Template names preserve the existing definitions: “Lapsing, opted in” includes
email-permitted past givers according to its saved rules; it is not the same
population as the narrower current `donor_status='lapsing'` group.

Evidence: `backend/.cache/relative-seed-acceptance/imports-r5pkot40/segments.json`
and `seed-report.json`. The disposable database was removed after success.

Validation: **192 backend tests passed**, 405 deselected; **12 dashboard frontend
tests** and **8 date-range tests** passed. Production build and typecheck passed.
The isolated 50k acceptance above also passed separately. Full backend suite
and signed-in browser interactions were not rerun; the running login page was
checked after restarting the application.
