# Overview API (live aggregates)

`GET /api/dashboards/overview?from=YYYY-MM-DD&to=YYYY-MM-DD`
requires viewer or higher. Dates are inclusive. Default is current month through
today; reversed ranges and ranges over 3,660 days return 422. Prior is the
immediately preceding equal-length period. Presets (including the organization's
fiscal year) are frontend date choices, not server assumptions.

Existing `range`, `metrics`, and `charts` remain. Other dashboard tabs are
unchanged. New rendering should use `overview`:

- `kpis`: `giving`, `active_partners`, `retention_yoy`, `average_gift`. Each has
  `value`, `prior`, `change`, `change_pct` (null for zero prior), `delta_label`
  (`"New"` when only prior is zero, `"—"` when both zero, otherwise null),
  `sparkline: [{month: "YYYY-MM-01", value: number}]`.
  Exactly 12 chronological calendar-month points ending at the selected end
  month; the last point stops at the selected end date. Empty months are zero.
  Giving is sum of gift amounts; active partners is distinct giving profiles;
  average gift is sum/count. All three use the selected date range, NOT an
  implicit rolling year. Retention is the percent of partners giving in the
  same calendar-date range one year earlier who also gave in this range.
  Leap days clamp to February 28. Retention includes `denominator`, `retained`,
  and `prior_denominator`; zero denominator produces zero with denominator=0,
  so UI can explicitly say “No prior-year partners” rather than imply a measured
  retention rate. Prior retention uses prior range versus its own year-earlier
  range. Percentage KPI delta is relative percent, not percentage points.
- `stats.profiles.value`: current active-profile population.
  `stats.recurring_partners`: delta object (no sparkline), distinct partners
  with recurring gifts in the 365 days ending on the selected end date versus
  the previous month end.
  `stats.email_opted_in`: `{value, percentage}` of current active profiles,
  with consent-ledger opt-outs and suppression exclusions.
  `stats.lapsing`: delta object comparing status on selected end date with the
  previous month end. “This month” refers to the selected end month.
- `monthly_giving`: one row per current calendar-month intersection:
  `{month, from, to, prior_from, prior_to, amount, prior_amount, gifts}`.
  Prior intervals align by elapsed days from each range start, not by calendar
  month names; their union exactly covers the prior interval. Partial months
  stay partial; missing months have zero. Nov–Dec styling uses `month`.
  `top_two_month_share`: percent of total giving in the two largest current
  buckets; null when total is zero. No canned takeaway.
- `campaigns`: top five ordered by total, gift count, name:
  `{campaign, share, gifts, average_gift, amount}`. Share is percent of ALL
  selected-period giving, not just the top five. `campaigns_href: "/?tab=giving"`.
- `partner_status`: `{givers, statuses: [{status, count, share}], prospects}`.
  Statuses are exclusive and ordered `active,new,reactivated,lapsing,lapsed`;
  share is percent of givers only. Evaluated from resolved gift history through
  selected end date. New: first gift within 365 days and last within 365;
  reactivated: last within 365 with >730 days since the preceding gift;
  active: other last gift within 365; lapsing: last 366–730 days ago;
  lapsed: last >730 days ago. New takes precedence over reactivated.
  Prospects are current active profiles with no gift through the selected end.
- `attention`: at most five `{severity,title,explanation,href}` rows, sorted
  error before warning before notice before healthy. No synthetic healthy row.
  Real unresolved records, unapproved high-cardinality identifiers and lapsing
  counts are viewer-safe; failed imports are analyst/admin-only; failed jobs
  are admin-only. Links target existing pages; import query identifies an
  import but the current imports page may require selecting it in its list.
  `/segments` is the existing segment workspace (no invented reactivation ID).

All overview giving queries JOIN `active_profiles` to `gifts.profile_id`.
Unresolved gifts, deleted profiles and merged losers are excluded. Counts are
distinct profiles, never source-record counts. No profile identifiers, emails,
raw job errors, filesystem paths or import filenames enter viewer responses.
Current profile/opt-in population is explicitly current, not a historical
snapshot. Existing compatibility `metrics.donors` is all-time givers through
the selected end; `giving_12m`/`active_donors_12m` remain 365-day metrics.
Compatibility aliases retain legacy keys; user-facing copy should say partners.
Amounts preserve existing single-ledger currency semantics; no FX conversion.

## Shell

`GET /api/shell` requires viewer or higher:

```
{
  imports_running: number | null,
  active_import: null | {
    id, name, done, total, percent: number | null,
    rows_per_second: number | null,
    speed_basis: "committed_rows_since_import_started",
    href
  },
  open_issues: number,
  issue_counts: {unresolved: number, review: number}
}
```

Viewer gets null for both import fields: hide restricted import UI. For
analyst/admin, running counts imports with both running import state and a
running import job; queued work is not “running”. Card selects oldest running
import. Done is persisted accepted+rejected rows, total is inspected CSV rows;
speed is that committed count divided by measured elapsed seconds since import
start (lifetime average, including pauses/retries, not instantaneous speed).
Unknown/zero elapsed yields null speed, unknown/zero total yields null percent.
No job payload, diagnostic error, or filesystem path is returned.
Open issues sums unresolved source records and unapproved high-cardinality
identifiers (same Data Health categories); it is not a count of attention cards.

## Freshness

Dashboard aggregates use a bounded host-shared file cache, namespaced by database
and suppression-hash configuration. Logical keys include dates and dashboard
name, independent of the PostgreSQL MVCC snapshot stored with each generation.
A generation already published when a request starts requires an exact snapshot
match (a durable, conservative changing transaction version). Commits by import,
identity, traits, or other processes invalidate these preexisting entries without
process-local invalidation hooks. Uncommitted session writes
bypass caching. Entries expire after 60 seconds; at most 32 entries of at most
4 MiB each are retained. Responses are copied; role-specific attention is added
after fetching shared aggregates. Errors are never cached.

A per-logical-key cross-process file lock deduplicates cold computations on a
host. Different dashboards/date ranges do not share a computation lock. A joining
request waits within its remaining 25-second budget and can adopt a generation
published during that call despite intervening commits. This is explicitly an
**as-of-computation** result, not a latest-at-response guarantee or a consistent
multi-query transaction snapshot. It does not grant TTL-based reuse of arbitrarily
old generations: subsequent requests still validate their snapshot. Heartbeats
do not force joined callers to immediately run a second expensive refresh.
Exhausted budgets return explicit 504, never a fake empty result. Failed owners
release the OS lock without publishing; a waiter can retry within its budget.
Publication is atomic, staging cleanup is key-local, and zero-byte lock inodes
are retained (never pruned, since waiters may hold them); payload bounds apply
to JSON entries. Multiple hosts remain correct but do not share this lock/cache.
Constant import commits can invalidate sequential requests: warm-cache latency
is **not** the acceptance criterion during resolution.

The overview combines all KPI periods, rolling compatibility metrics, 12 sparkline
points, and historical classifications in one profile-grain gift aggregation.
A second date-bounded scan supplies daily paired totals and campaign totals with
GROUPING SETS. No gift/source/consent fanout or per-month full-history scans remain.
Consent counts reduce source records to profile flags when no suppressions exist.
With suppressions, distinct candidate addresses are streamed in 2,048-row batches
and hashed with the writer's Unicode casefold semantics. Ledger opt-outs override
all source opt-ins; any unsuppressed eligible address qualifies the active profile.

Dashboard and shell work have a 25-second computation budget. Each database
statement and streamed fetch receives the remaining statement timeout; database
lock waits are bounded to two seconds (not cache-coalescing waits).
Timeout/lock cancellation returns explicit 504.
No migration, production seeding, worker changes, or recompute is required.

## Isolated performance evidence

Measured on the final implementation using the disposable PostgreSQL 17 harness
in `app.benchmark`, not the app/development/production database. Unprofiled Python
3.12 processes, shared cgroup quota `400000 100000` (4 CPUs), 8 GiB memory limit.
PostgreSQL retained normal durability; WAL retention was lowered to 128 MiB maximum/
32 MiB minimum during fixture recovery to reduce scratch usage, not disable fsync.

Committed fixture: **5,360,000 gifts, 2,900,000 source records, 250,000 active
profiles**. The suppression run also had **250,000 consent rows and one hard-bounce
suppression**. Gifts cover six years, eight campaigns, recurring and non-recurring
gifts; each profile has repeated source records. Ten percent of seeded sources
initially have unresolved timestamps.

| Final measurement | Seconds |
|---|---:|
| Cold viewer endpoint, suppression + ledger present | 17.457 |
| Subsequent shared-cache hit, including live attention | 0.0075 |
| Cold endpoint during concurrent synthetic resolution writes | 17.029 |
| Second endpoint call, invalidated again by concurrent writes | 21.734 |

These measure the complete viewer endpoint function, including live attention,
but exclude HTTP transport and authentication middleware.
The separate writer committed 79 batches of 1,000 source-record timestamp updates
with half-second pauses between transactions during the two-request experiment. This
exercises durable invalidation and write contention; it is **not** the real identity
resolver and does not establish performance under its full CPU/IO load.
“Cold” means no aggregate cache entry, not cold operating-system page cache.

Correctness assertions checked committed row counts, exact giving against an
independent scalar SQL sum (**8,924,250** for 2025), **249,999** eligible profiles
with the suppression, and identical aggregate payloads on repeated requests
(live attention can change during resolution). Earlier,
the no-ledger/no-suppression fixture measured 14.489 seconds and 250,000 eligible
profiles. That earlier timing is diagnostic, not the final suppression workload.

**Limitation:** the planned 6,000,000-source fixture did not finish within the
execution tool's five-minute seed limit. The committed disposable fixture was
recovered instead of repeatedly reseeding. Thus full 6m-source acceptance and a
production-real-resolver concurrency claim remain unverified. All measured cold
requests above completed within 25 seconds; failures at higher load remain
explicit, not cached or silently replaced.

Raw workspace-local evidence:
`/tmp/overview-performance-evidence/imports-g9gm0xz2/final-viewer-endpoint/overview-results.json`
and sibling `final-viewer-concurrent/overview-results.json`. The disposable cluster
was stopped after measurement. To reproduce on a host without the tool's five-minute
command limit:

```sh
cd artifacts/audience-hub/backend
python -m app.cli benchmark --overview --gift-rows 5360000 \
  --contact-rows 6000000 --output-dir /tmp/overview-evidence
```

The harness checks the isolated URL before writing and commits fixture inserts in
100k batches. It runs dashboard regression tests before seeding and removes its
cluster on normal completion. For a fast isolated regression-only-sized run, use
`--gift-rows 1000 --contact-rows 1000`. Tests cover default/no-history ranges,
leap dates, retention, excluded merged/deleted profiles, duplicate source records,
Unicode suppression, opt-outs, alternate eligible emails, process-visible commit
invalidation, cache expiry/bounds/copy safety/deduplication, and real SQL cancellation.
Post-coalescing isolated dashboard regressions: **26 passed**, including all
dashboard-tab SQL smoke queries (no skipped tests). The concurrency regression
runs four independent API processes against one logical key while a separate
process commits real profile updates; all four receive one owner's identical
payload, with exactly one expensive build. A later committed traits insert
invalidates the preexisting generation. Separate regressions cover independent
dashboard/date locks, deadline exhaustion, owner failure/recovery, flushed-write
bypass, oversized payloads, expiry and bounds. This is a small correctness
fixture, not a replacement scale benchmark; the 17–22-second evidence above
remains the cold-computation measurement.

A separate targeted disposable-PostgreSQL regression also passes: the real
`/api/shell` handler completes with a 750 ms budget while another process holds
the Overview computation lock. Its `("health-counts",)` cache key is independent
of `("overview", start, end)`. Overview attention reads health counts only after
the aggregate cache call returns and releases its lock; no nested computation
locks are held on that path.