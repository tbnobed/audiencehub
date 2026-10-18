# Audit seed gift distribution

This change affects **future audit-mode seed generation only** and extends the
read-only `kinship verify-seed` report. It does not repair, rescale, reset or
rewrite the existing production ledger. Explicit legacy seed mode is unchanged.
No migration is required.

## Targets and interpretation

At 500,000 requested people the generator budgets 345,000 positive original
donations plus approximately 3,150 separately linked return rows
(`27 / 2,957` of original donations). Budgets scale with `--profiles`, not with
the seed density label. Extremely small seeds necessarily round cohorts/quotas;
required audience members take priority if the integer budgets conflict.

- Approximately 35% of donors retain exactly one gift. Other basic subscribers
  cannot accidentally acquire imported `rfm_raw_frequency=1`, which would expand
  Basic One-Time Donors.
- The remaining population starts with at least two retained gifts and receives
  weighted extra gifts, including a small 20+ gift group. Recurring partners get
  a monthly gift in every eligible completed month of the five-year window.
  Additional voluntary gifts are one-time, not extra monthly recurring bills.
- The last-30-day positive-donation budget is
  `round(profiles * 2957 * 30 / 7830000)` (about 5,665 at 500k, or 189/day).
  Repeat gifts are allocated to the **existing** recent recipient cohort; they
  do not manufacture additional Donors Past 30 Days or TBN App Donors.
- Body amounts are log-normal, centered to keep the combined median near $50.
  Stratified gift-ordinal quotas allocate 1.5% of donations at $1,000+,
  including 0.05% at $10,000+, with a $50,000 maximum when that tail is populated.
  Amounts remain currency units in CSV/events and integer cents internally.
- Every return references one original gift belonging to the same person,
  reverses its exact amount, occurs no earlier than the original, and is emitted
  at most once. Returns do not remove protected audience-defining gifts or leave
  a multi-gift basic donor with just one retained gift. Recent returns retain
  the audit's separate `27/day at 7.83M` scaling.

## Audience and date invariants

People, contact/source assignment, identity evidence, key proportions and
overlap rules are unchanged. All existing audience cohort selectors are
unchanged. Giving Tuesday remains December 3, 2024 on its four existing codes
when that date is inside the five-year window.

Every generated date remains between the calendar `as_of - 5 years` anniversary
and yesterday. `--as-of` behavior/default UTC reference is unchanged. Recurring
schedules end in the last completed month.

Histories are generated within each donor's accepted cohort constraints:

- Existing recent recipients have protected, unreturned recent donations.
- Other TBN+ subscribers do not acquire donations within 720 days.
- The same imported RFM-lapsed members have no donations within 730 days.
- Non-lapsed imported RFM includes both active and **lapsing** partners; it does
  not imply a gift within 12 months.
- All existing givers retain positive lifetime giving, and non-givers acquire
  no gifts.

Imported RFM raw frequency/monetary/recency, score components, model segment and
last-gift summaries are derived from the retained history. Lifetime amount is
the net signed ledger sum. Basic One-Time membership and the RFM-lapsed
membership remain fixed, not merely within acceptance tolerance.

## Seasonality and diagnostics

Date sampling weights November/December at 30% of available annual one-time
exposure, includes a summer dip and a last-week-of-December spike. Fixed recent
rates, protected cohort dates and donor inactivity constrain which calendar
years/months can contain gifts. The amount distribution is sampled independently
of month; actual dollar shares must be checked, not inferred from date weights.
Five rolling years span partial boundary calendar years, and a highly lapsed
population also truncates the later years of most individual histories.

`kinship verify-seed` remains PostgreSQL-enforced read-only. It now prints:

- Gift frequency median/p95 for all positive originals and for unreturned gifts.
- One-time-giver share and the count with 20+ unreturned gifts.
- $1,000+ and $10,000+ counts/shares.
- Return count and percent of original donations.
- November/December share of unreturned one-time dollars per year, observed date
  bounds, and the aggregate share.

Distribution figures are diagnostics; the existing PASS/FAIL still refers to
ledger reconciliation checks, not a claim that realism targets have passed.
New regression coverage in `test_seed_audit.py` checks exact quotas, protected
memberships, monthly schedules, one-to-one refunds and imported model/ledger
consistency, reusing the existing audit seed fixture.

**No tests, seeds, imports or benchmarks were run in Replit for this change.**
The listed targets and algorithm are not production measurements. Verification
and any deliberate future seed replacement belong on the production server.
