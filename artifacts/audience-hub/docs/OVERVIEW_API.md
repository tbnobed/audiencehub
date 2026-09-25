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

Dashboard response caching is disabled, including other tabs. Every API process
reads committed database state per request; recompute/merge/import commits
cannot leave another process's 300-second cache stale. No migration, worker
change, import rerun, or recompute is needed. `invalidate_cache()` remains a
compatible hook. Tradeoff: more aggregate queries rather than stale results.