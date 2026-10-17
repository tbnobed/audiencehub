# Production 500k load review

## User-reported production completion

The production console reported:

| Measure | Result |
| --- | ---: |
| Profiles | 491,088 |
| Existing-profile merge events | 0 |
| Gifts | 5,360,521 |
| Selected unresolved contacts | 0 |
| Global unattached gifts | 0 |
| Global unattached events | 388 |
| Cached metrics age | 0 seconds |
| Load-status wall clock | 4,058.6 seconds (67m 38.6s) |
| Shell end-to-end elapsed | 4,829.931 seconds (80m 29.931s) |

**Completion: PASS. Under-60-minute production target: NOT MET.**
These are user-supplied production results, not measurements repeated in Replit.
The reported completion includes tracked identity, traits and rollups; the CLI
also reported computing traits and backfilling 24 monthly snapshots.

The two clocks measure different scopes. Their difference is 771.331 seconds
(12m 51.331s); without phase logs it cannot be attributed entirely to any one
operation. Do not present load-status elapsed as the shell's full-load duration.

Unattached events are visible exceptions, not unresolved-contact backlog. The
provided output does not establish why each of the 388 events is unmatched.
Zero existing-profile merges can be normal for an initial full connected-
components pass: matching source records may be assigned directly to one new
profile. This output alone does not verify multisource coverage or ground-truth
accuracy; those require the separate accuracy report.

## Scope of this correction

Dashboard correctness and seed realism are addressed before further milestone
work. Existing production data is not reset, regenerated or modified. New seed
distributions apply to newly generated demo fixtures; they do not retroactively
change an already imported dataset.

## Corrections and verification

- Retention uses adjacent trailing calendar-year windows anchored at the selected
  end date, not overlapping five-year populations. The tooltip consumes the
  server's explicit dates, including leap-year boundaries.
- Multi-year giving folds calendar months across years and names distinct
  months. The takeaway uses the same totals as the displayed chart.
- Partner status, Lapsing and Needs attention use the same published current
  `profile_traits` status. Historical selections label these as “as of today”;
  unavailable historical status deltas are not rendered as invented changes.
- Giving, Retention, Engagement and Sources use published rollups. Tests compare
  every chart with independent SQL; source diagnostics remain all-time. Giving
  dimensions consistently use active, resolved profiles.
- Rejected-row reports use the same unreviewed completed/failed-import query in
  Overview and Data Health, independent of the selected gift dates.
- All tabs use consistent cards and compact empty states. Dashboard cache keys
  exclude pre-correction payloads.

Verification performed in isolated/local environments:

| Check | Result |
| --- | --- |
| PostgreSQL dashboard/health regression suite | 43 passed |
| Seed generation and realism tests | 15 passed |
| Frontend dashboard regressions | 11 passed |
| Frontend TypeScript check and production build | PASS |

PostgreSQL evidence:
`backend/.cache/review-dashboard-isolated/imports-3eggk1xy/pytest.txt`.
The database tests include hand-computed fixtures, raw-table query guards,
independent SQL comparisons and cache-generation consistency checks.
The full backend suite was not rerun for this correction.
Signed-in dashboard browser interaction was not verified; frontend rendering
and calculations were checked through component tests.

The measured 50,000-person seed report, campaign shares, gift-size distribution,
annual seasonal shares, retention and reproduction commands are in
[SEED_REALISM_VERIFICATION.md](SEED_REALISM_VERIFICATION.md). This is generated
CSV evidence, independently checked with SQLite, not a new production import.

### Applying to the existing production dataset

No production deployment or database writes were performed here. After deploying
the corrections, publish refreshed rollups using the existing full
`traits.recompute` job procedure in [DEPLOYMENT.md](DEPLOYMENT.md); existing
rollups predate the corrected resolved-population aggregation. Do not reset or
reimport the production dataset merely to apply dashboard corrections.
