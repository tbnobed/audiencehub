# Dashboard API: published rollups

All dashboard requests require viewer or higher; CSV requires analyst. Requests
never scan raw `gifts`, `events`, or `source_records`, including cold cache misses,
card endpoints, other tabs, and CSV downloads. No email HMAC/UNION is performed
on request. Consent is authoritative only in `consents`; ingestion and identity
merge hooks materialize it before the offline dashboard refresh.

## Refresh and readiness

Migration `0020_dashboard_rollups` follows `0008_import_checkpoints`;
`0021_consent_channel_status` follows it. Upgrade with Alembic before starting
new workers. A full `traits.recompute` publishes the initial dashboard generation.
Until then endpoints return **503** with explicit refresh instructions rather
than fabricated empty data.

`traits.recompute` refreshes the rollups in the same transaction as traits. This
runs nightly and after import identity processing drains; incremental trait
recomputation also republishes the aggregates. Failed/rolled-back refreshes leave
the previously committed generation intact. Readers see committed generations
through MVCC, without waiting on `TRUNCATE`/DDL.

- `dashboard_daily(day, source_id, fund, campaign, channel, gift_count,
  gift_amount, donor_count, new_donor_count)` also separates active/inactive
  populations so Overview excludes deleted/merged/unresolved profiles while
  Giving retains its all-gift totals.
- `dashboard_kpis(as_of, key, value)` holds population, authoritative opted-in
  count, current trait status/recurring/lapsing aggregates, precomputed diagnostic
  charts, annual retention/cohorts, and the published generation.
- `dashboard_donor_daily` retains exact profile/day facts. Lossless
  `dashboard_donor_patterns` groups identical dated histories (including gift
  multiplicity and recurring flags) with a donor weight. Date-range distinct
  donors and retention are computed at this membership grain: **daily donor
  totals are never summed**. Same-day repeat gifts remain separate gifts, not
  separate donors or false reactivations.
- Giving distribution/appeal, event/day, and conversion/day tables retain the
  remaining chart dimensions without request-time raw scans.

Pattern compression is lossless, not sampling or approximate distinct counting.
Its performance depends on temporal-history diversity; the synthetic benchmark
is not a guarantee for every production distribution.

## Shared stale-while-revalidate

`dashboard_cache(key, payload jsonb, computed_at, generation)` is shared by all
API workers. Entries expire after 60 seconds or when traits publishes a new
generation. Existing entries are returned immediately; background work uses a
PostgreSQL advisory lock to coalesce refreshes and retains stale data if a refresh
fails. There is no process-local or filesystem payload cache.

Cold builds read only rollups in a repeatable-read snapshot. Publication checks
the current generation under a nonblocking shared refresh lock; obsolete builds
cannot overwrite new-generation data. A running offline refresh never blocks a
request just to publish its cache entry.

## Date and response contracts

Dates are inclusive: `?from=YYYY-MM-DD&to=YYYY-MM-DD`. Default is current month
through today. Reversed ranges and ranges exceeding 3,660 days return 422.
Prior means the immediately preceding equal-length period.

`GET /api/dashboards/overview` retains `{range, metrics, charts, overview}`.
`overview` retains:

- `kpis`: `giving`, `active_partners`, `retention_yoy`, `average_gift`, each with
  numeric `value`, `prior`, `change`, nullable `change_pct`, `delta_label`, and
  exactly twelve `{month,value}` sparkline points. Retention intersects donors
  in the selected range and the same calendar dates one year earlier (leap-day
  clamping), and includes `denominator`, `retained`, `prior_denominator`.
- `stats`: profiles, email opted-in/percentage, recurring partners, lapsing.
  Historical recurring/status comparisons are derived from published temporal
  membership facts, not today's status substituted for historical end dates.
- `monthly_giving`: `{month,from,to,prior_from,prior_to,amount,prior_amount,gifts}`;
  prior buckets partition the prior range by elapsed days. Missing months are
  zero. `top_two_month_share` is null if giving is zero.
- `campaigns`: top five `{campaign,share,gifts,average_gift,amount}`;
  `campaigns_href`; `partner_status`: `{givers,statuses,prospects}`, with ordered
  exclusive active/new/reactivated/lapsing/lapsed statuses.
- `attention`: role-safe actionable issues and lapsing partners.

Progressive card routes preserve these nested field types:

| Route suffix | Response |
|---|---|
| `/overview/kpis` | `{range, kpis, stats}` |
| `/overview/giving-by-month` | `{range, giving_by_month: {monthly_giving, top_two_month_share}}` |
| `/overview/needs-attention` | `{attention}` |
| `/overview/campaigns` | `{top_campaigns: {campaigns, campaigns_href}}` |
| `/overview/partner-status` | `{partner_status}` |

The legacy `/overview/primary`, `/overview/email`, `/overview/attention`, all six
dashboard tabs, and `/{dashboard}/{chart}/csv` remain available.

## Reproducible scale evidence

Run without a browser or application database:

```sh
kinship benchmark --dashboards --profiles 500000
kinship benchmark --dashboards --test-suite
```

The harness creates/migrates/stops/removes its own private PostgreSQL cluster
and refuses production. It preserves JSON timing evidence and logs. Benchmark
requests use real dev login and authenticated ASGI TestClient, including routing,
middleware, signed session/auth, DB session cleanup and JSON/CSV serialization;
they exclude network latency. Each endpoint is measured with a cleared shared
cache and then warm. All chart CSV routes are included. SQL listeners reject
any request query referencing a forbidden raw table.

Measured 500,000 profiles, 2,000,000 gifts, 500,000 source records, and 500,000
consents. Exact giving (15,000,000), distinct donors (500,000), retained donors
(500,000), and email opted-in (500,000) assertions passed. Four gifts/profile
include same-day repeat gifts and a prior-year gift. Repeated temporal histories
compress exactly; this limitation is explicit in the evidence.

Final evidence: backend `.cache/dashboard-http-verified/imports-xioe2yrd/dashboards.json`.
Refresh: 31.681 seconds (offline, not included in request time).

| Endpoint | Cold ms | Warm ms |
|---|---:|---:|
| overview | 263.30 | 17.05 |
| giving | 32.08 | 6.37 |
| retention | 27.48 | 7.34 |
| engagement | 33.69 | 7.02 |
| sources | 158.34 | 5.69 |
| data-health | 17.60 | 7.14 |
| overview/kpis | 48.03 | 6.30 |
| overview/giving-by-month | 46.93 | 7.20 |
| overview/needs-attention | 7.71 | 7.68 |
| overview/campaigns | 53.35 | 10.81 |
| overview/partner-status | 55.62 | 7.05 |
| overview/email | 6.90 | 5.88 |
| overview/attention | 6.75 | 8.11 |
| overview/primary | 48.04 | 8.47 |

All measured JSON and CSV endpoints passed the 300 ms target. Full per-CSV
timings are in the JSON evidence and printed by the command.

Final migrated disposable PostgreSQL suite: **249 passed, 1 skipped**, 91.26
seconds; evidence `.cache/dashboard-tests-final/imports-l65nytuv/pytest.txt`.
Consent schema tests, all dashboard regressions, and worker recovery tests ran
against this database. The skipped test is explicitly opt-in.

An intervening benchmark launched concurrently with the full suite exceeded the
300-second harness execution budget before printing endpoint results. It is not
counted as passing evidence; the final successful benchmark above ran separately.