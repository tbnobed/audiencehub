# Seed realism verification — production review items 7–10

## Scope and evidence

Changed **CSV seed generation only** (`backend/app/seed.py` and the new
`seed_giving.py` helper), with an independent CSV reader (`seed_report.py`) and
regression tests. Import, identity-resolution, gift-attachment, dashboard and
frontend algorithms were not changed. No database connections, seed loading,
workflow restarts, or live-data changes were used for this verification.

The measured run used **50,000 requested people, small density, seed 20250308**,
writing all five real seed CSVs to the isolated directory
`/tmp/audience-hub-seed-realism-final`. Generation plus CSV measurement took
**34.77 seconds** in this development environment; this is not production import
throughput evidence.

| File | Physical data rows |
|---|---:|
| donor_crm_contacts.csv | 50,497 |
| giving_platform_gifts.csv | 177,010 |
| esp_contacts.csv | 25,135 |
| five9_calls.csv | 3,733 |
| zeta_enrichment.csv | 50,002 |

The giving report excludes exactly **one identical duplicate external ID** and
**one deliberately invalid, blank-identity sentinel**. Its population is
**177,008 unique valid gifts**, totaling **$22,156,195.85**. It parses the actual
mixed-format CSV dates and sums integer cents; it does not read generator
targets, generator statistics, or the campaign model. Unexpected invalid rows
and conflicting duplicate IDs raise explicit errors rather than disappearing.

Gift CSV SHA-256:
`a04770a72b59f122b2ed7756481792bcf8f792fe30b8d75aa1e9f1afa1355115`

The complete machine-readable report is available for this run at
`/tmp/audience-hub-seed-realism-final/measured-report.json`. Temporary files are
not committed; the commands below reproduce the evidence.

**Important limit:** these are pre-import CSV measurements. Retention is by
`contact_external_id` (the generated CRM person reference), not by email, gift
ID, or an imported profile ID. Household merges or other normal importer
outcomes have not been measured here. This report does not claim that a
production database, imported dashboards, or signed-in UI were verified.

## Model changes

- Twelve campaigns, weighted by **dollars**, not uniformly selected labels.
  Year-End receives 32%, Monthly Partner Program 20%, and the remainder tapers.
- One-time gift sizes are log-normal draws (log-space standard deviation 1.65),
  with distinct campaign and channel multipliers. Mail has a lower multiplier
  than online; a rare major-gift channel has a much larger multiplier.
  Monthly plans use a narrower log-normal distribution and one stable payment
  amount/day throughout each calendar year. The old $25,000 truncation is gone.
- Annual campaign/season dollar budgets rescale those draws. Thus the resulting
  data is a **calibrated mixture of log-normal populations**, not a claim that
  every pooled gift is an unconditioned draw from one log-normal distribution.
  The calibration intentionally prevents a few large gifts from overwhelming
  the campaign shares, annual growth, or seasonal shape.
- Year-End starts in September; 60% of its one-time dollars land in November
  and December. Other one-time campaigns put 10% there, giving a combined 30%.
  Other-campaign month weights dip in June–August. December dates concentrate
  in December 25–31. Monthly gifts are flat, **not seasonally weighted**.
- Annual donor cohorts grow approximately 4.5%. Exactly approximately 45% of
  prior-year donors return (integer rounding applies); new/reactivated donors
  replace churn. Existing monthly partners are stickier, but can still lapse.
  Cohort construction excludes previous-year non-retained partners from the
  replacement pool, preventing accidental inflation of adjacent-year retention.
- Density changes gift frequency; its expected-count budget adjustment keeps
  the typical gift size near $50 across small, medium, and large presets.
  Only one year's giving rows are buffered. Tiny fixtures still work, but
  cannot meaningfully represent all campaigns or population-level targets.

## 1. Measured campaign shares

Shares below are of **all valid giving dollars**, including recurring gifts.

| Campaign | Gifts | Dollars | Share | Average gift |
|---|---:|---:|---:|---:|
| Year-End Giving | 38,852 | $7,089,983.38 | 32.000003% | $182.49 |
| Monthly Partner Program | 70,896 | $4,431,235.92 | 19.999985% | $62.50 |
| Spring Outreach | 14,830 | $2,481,494.19 | 11.200001% | $167.33 |
| Global Missions | 12,950 | $1,949,745.57 | 8.800002% | $150.56 |
| Broadcast Ministry | 8,119 | $1,595,246.62 | 7.200002% | $196.48 |
| Community Relief | 8,590 | $1,240,747.10 | 5.600001% | $144.44 |
| Easter Hope | 6,447 | $886,248.34 | 4.000002% | $137.47 |
| Summer Youth | 5,612 | $708,998.28 | 3.200000% | $126.34 |
| Bible Translation | 2,786 | $620,373.67 | 2.800001% | $222.68 |
| Food for Families | 3,587 | $443,124.31 | 2.000002% | $123.54 |
| Prayer Outreach | 3,068 | $354,499.30 | 1.600001% | $115.55 |
| Legacy Impact | 1,271 | $354,499.17 | 1.600000% | $278.91 |

Cent rounding explains the very small departures from budget percentages.

### Measured channels

| Channel | Gifts | Dollars | Share of all dollars | Average gift |
|---|---:|---:|---:|---:|
| online | 116,497 | $14,239,224.11 | 64.267459% | $122.23 |
| mail | 45,600 | $5,107,856.82 | 23.053853% | $112.01 |
| phone | 10,688 | $1,708,451.31 | 7.710942% | $159.85 |
| event | 4,162 | $720,621.11 | 3.252459% | $173.14 |
| major_gift | 61 | $380,042.50 | 1.715288% | $6,230.20 |

Online includes all 70,896 lower-value recurring payments. Comparing **one-time
gifts only**, online averages **$215.08** (45,601 gifts), versus mail **$112.01**
(45,600 gifts). Major gifts are only **61 / 177,008** gifts (approximately
0.0345%); the channel is not a synonym for every gift of $1,000 or more.

## 2. Measured gift-size distribution

| Amount band | Gifts | Share of gifts |
|---|---:|---:|
| Under $25 | 45,362 | 25.6271% |
| $25 to under $50 | 43,072 | 24.3334% |
| $50 to under $100 | 49,368 | 27.8903% |
| $100 to under $500 | 32,140 | 18.1574% |
| $500 to under $1,000 | 4,122 | 2.3287% |
| $1,000 to under $10,000 | 2,880 | 1.6270% |
| $10,000 or more | 64 | 0.0362% |

- **Median: $50.04**
- 90th percentile: **$205.80**; 99th percentile: **$1,454.53**.
- **2,944 gifts of $1,000 or more: 1.663202%**.
- **64 gifts of $10,000 or more**; maximum **$52,190.03**.
- Percentiles use the lower empirical order statistic; median is the ordinary
  midpoint median.

## 3. Measured seasonality and year-over-year growth

| Year | All dollars | One-time dollars | One-time Nov–Dec dollars | Nov–Dec: one-time share | Nov–Dec: all-gift share | All-dollar growth |
|---|---:|---:|---:|---:|---:|---:|
| 2020 | $4,050,000.89 | $3,240,000.41 | $972,000.22 | 30.000003% | 27.333335% | N/A |
| 2021 | $4,232,250.10 | $3,385,800.46 | $1,015,740.06 | 29.999998% | 27.333333% | 4.499980% |
| 2022 | $4,422,601.35 | $3,538,080.27 | $1,061,424.09 | 30.000000% | 27.333331% | 4.497637% |
| 2023 | $4,621,495.87 | $3,697,199.59 | $1,109,159.66 | 29.999994% | 27.333337% | 4.497229% |
| 2024 | $4,829,847.64 | $3,863,879.20 | $1,159,163.69 | 29.999998% | 27.333335% | 4.508319% |

The **all-gift share is lower than the one-time share**, as expected: recurring
giving is flat and represents about 20% of annual dollars. It is not correct
to label the all-gift share as 30%.

| Year | Dec 25–31 share of one-time December dollars | Jun–Aug average monthly one-time dollars | Jan–May average monthly one-time dollars | Recurring dollars in EACH month |
|---|---:|---:|---:|---:|
| 2020 | 58.177744% | $111,654.10 | $187,749.83 | $67,500.04 |
| 2021 | 54.751933% | $113,254.42 | $211,193.12 | $70,537.47 |
| 2022 | 59.398440% | $119,845.90 | $209,768.43 | $73,710.09 |
| 2023 | 53.688002% | $98,379.64 | $239,935.07 | $77,024.69 |
| 2024 | 54.343457% | $120,058.10 | $236,168.43 | $80,497.37 |

All **5,908 generated monthly plans** have twelve payments, one per month,
with an identical amount for that plan/year. Flatness is within each year;
annual recurring dollars grow with the donor cohort. More than half of
one-time December dollars arrive in the last seven days, versus 7/31
(22.58%) for uniform daily giving.

## 4. Measured annual partner retention

Definition: distinct generated CRM donor references giving in both calendar
years, divided by distinct references giving in the prior year. Each comparison
uses two disjoint, adjacent twelve-month periods, not the whole five-year union.

| Giving year | Current-year donors | Prior-year donors | Retained donors | Retention |
|---|---:|---:|---:|---:|
| 2020 | 9,000 | N/A | N/A | N/A |
| 2021 | 9,405 | 9,000 | 4,050 | 45.000000% |
| 2022 | 9,828 | 9,405 | 4,232 | 44.997342% |
| 2023 | 10,270 | 9,828 | 4,423 | 45.004070% |
| 2024 | 10,733 | 10,270 | 4,622 | 45.004869% |

There is no fabricated 2019 giving cohort; 2020 retention is undefined, not
0% or 100%. Donors can reactivate after skipping a year.

## Independent cross-check and regression tests

In addition to the CSV report, the actual gift CSV was read separately into
an **in-memory SQLite database** (unique external ID key, actual parsed dates,
integer cents). This did not involve the application database or importer.
Direct SQL independently matched:

- Count/sum: **177,008 gifts / 2,215,619,585 cents**.
- Every campaign gift count and dollar sum in the table above.
- Each year's one-time Nov–Dec numerator and one-time annual denominator.
- Retention denominators/numerators: **9,000/4,050; 9,405/4,232;
  9,828/4,423; 10,270/4,622**.

For example, retention was independently computed from distinct `(donor, year)`
rows, counting a retained donor only when an `EXISTS` query found the same
donor in `year + 1`. These checks measured generated rows rather than echoing
the configured budget fractions.

Executed:

```sh
cd artifacts/audience-hub/backend
python -m pytest tests/test_seed.py tests/test_seed_realism.py -q
python -m compileall -q app/seed.py app/seed_giving.py app/seed_report.py tests/test_seed_realism.py
```

**Result: 15 passed in 8.00 seconds; compilation succeeded.** Tests cover:
campaign dollar shares/count/taper/distinct averages; channel differences and
rare major gifts; median and high-value tail; per-year seasonal shares;
December final-week spike; summer dip; flat monthly plans; 3–6% annual growth;
40–50% retention with real churn; deterministic generation; tiny fixtures;
size/tail preservation at all three densities; independent hand-computed
report totals/retention; and loud failure on unexpected malformed data or
conflicting duplicate IDs. Existing seed CLI/no-database/messiness tests pass.

## Reproduce without loading or touching live data

From the workspace root:

```sh
export PYTHONPATH="$PWD/artifacts/audience-hub/backend"
export REALISM_DIR="$(mktemp -d /tmp/audience-hub-realism-XXXXXX)"
python - <<'PY'
import os
from app.seed import generate_seed
generate_seed(profiles=50000, scale="small", random_seed=20250308,
              output_dir=os.environ["REALISM_DIR"])
PY
python -m app.seed_report "$REALISM_DIR/giving_platform_gifts.csv" \
  > "$REALISM_DIR/measured-report.json"
sha256sum "$REALISM_DIR/giving_platform_gifts.csv"
```

Do not add `--load` for this isolated verification. The changed generator does
not retroactively modify any existing database or CSVs. A future authorized
clean import is required before claiming imported-profile or dashboard results.
