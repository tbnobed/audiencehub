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
- The last-30-day positive-donation budget is approximately **1.4 per existing
  recent donor** (about 1,698 for the same 1,213 people at 500k). Protected monthly
  bills can raise this within the 1.2–1.6 range. This newer requirement supersedes
  the old 190/day target; it does not change the five-year total. Repeat gifts
  remain within the existing recent recipient cohort.
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

The calendar is now budgeted **before donor assignment**. Whole-window daily
weights include 4% underlying annual growth, a summer dip, 30% November/December
exposure, the annual Giving Tuesday date and a last-week-of-December spike.
Boundary years receive only their actual days' weighted exposure; they are not
given full-year budgets. Calendar budgets include mandatory gifts rather than
adding mandatory gifts on top.

Lapsed partners receive their minimum gifts from the older portion of that
shared calendar. Their individual last dates become their stopping dates;
additional gifts cannot move those stops toward the 24-month boundary. The
active non-single cohort is reserved to carry later calendar slots, rather than
allocating nearly all repeat-gift capacity to lapsed people. Recurring monthly
dates remain explicit.

Two existing membership intersections necessarily constrain local dates:
non-recent TBN+ partners with nonzero imported RFM recency must remain outside
720 days but inside 730, and recent volume must obey the new per-person budget.
The allocator includes those protected dates in its budgets; it does not use
them to truncate the population's entire timeline.

After generating amounts and assigning full linked returns, same-year swaps
between unreturned one-time gifts balance November/December dollars to 25–35%.
Swaps preserve the amount multiset, annual gift/dollar totals, all dates, tail
quotas, return links and recipient cohorts. Both positive-original and
unreturned-dollar shares are checked. Very small cohorts can make a dollar
interval impossible because one immutable tail amount dominates a year; they
retain their amounts and expose the discrepancy in diagnostics. Plans at
50,000+ people refuse to write CSV files if the full-year acceptance checks fail.

`kinship verify-seed` remains PostgreSQL-enforced read-only. It now prints:

- Gift frequency median/p95 for all positive originals and for unreturned gifts.
- One-time-giver share and the count with 20+ unreturned gifts.
- $1,000+ and $10,000+ counts/shares.
- Return count and percent of original donations.
- November/December share of unreturned one-time dollars per year, observed date
  bounds, and the aggregate share.
- Positive-original and unreturned gifts/dollars by year, separating one-time
  and recurring gifts.
- The largest full-year YoY change in one-time and total positive gift counts.

Full years are determined from the UTC reference's five-year window, never
from the minimum/maximum observed gift date. `verify-seed --as-of YYYY-MM-DD`
must match an explicitly dated seed; the default reference remains today UTC.
The command prints a FAIL for every full year's one-time dollar share outside
25–35%, or full-year one-time/total gift-count change beyond ±25%. Missing full
years fail too. These failures now produce exit status 1 alongside ledger
failures. Amount/frequency/tail statistics remain descriptive.

New regression coverage in `test_seed_audit.py` checks exact quotas, protected
memberships, monthly schedules, one-to-one refunds and imported model/ledger
consistency, as well as annual volume, dollar seasonality and distributed lapsed
stopping years, reusing the existing audit seed fixture. Pure timeline tests
cover partial-year exclusion, missing years and inclusive failure thresholds.

**No tests, seeds, imports or benchmarks were run in Replit for this change.**
The listed targets and algorithm are not production measurements. Verification
and any deliberate future seed replacement belong on the production server.
