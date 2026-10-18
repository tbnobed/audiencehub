# Audit seed verification

Reference date: 2026-10-17. Fresh isolated PostgreSQL load; no live data touched.

Profiles: 50000. Unresolved contacts/facts: 0/0. Dashboard nonempty: true.

Audience expectations use 7,807,899 as denominator and max(20% expected,25 records) tolerance. The additional population/activity checks use their separately specified ratios. Queries are independent of the segment compiler.

| Metric | Actual | Expected | Tolerance | Pass |
|---|---:|---:|---:|---|
| All TBN Reference | 50000 | 50000.00 | 10000.00 | PASS |
| Has Active Email | 28659 | 28658.53 | 5731.71 | PASS |
| Active (120 days) | 18585 | 18584.94 | 3716.99 | PASS |
| TBN Plus Subscription | 13364 | 13363.37 | 2672.67 | PASS |
| TBN Plus Free Users | 13131 | 13130.78 | 2626.16 | PASS |
| TBN Plus Premium | 233 | 232.59 | 46.52 | PASS |
| Premium Upgraders | 56 | 56.16 | 25.00 | PASS |
| TBN App Non-Donors | 12295 | 12295.27 | 2459.05 | PASS |
| TBN App Donors | 108 | 107.94 | 25.00 | PASS |
| Basic Donors | 462 | 461.71 | 92.34 | PASS |
| Basic Recurring Donors | 30 | 29.83 | 25.00 | PASS |
| Basic One-Time Donors | 72 | 71.57 | 25.00 | PASS |
| RFM TBN All Donor Reference | 11268 | 11268.24 | 2253.65 | PASS |
| RFM Lapsed Donors | 10299 | 10299.13 | 2059.83 | PASS |
| Partner Names Non-Donors | 3827 | 3827.14 | 765.43 | PASS |
| Donors Past 30 Days | 121 | 121.31 | 25.00 | PASS |
| Living Legacy Prospects V2 | 5 | 5.05 | 25.00 | PASS |
| Giving Tuesday 2024 | 5 | 4.51 | 25.00 | PASS |
| Mobile Subscribed | 571 | 571.22 | 114.24 | PASS |
| Call Center SMS Opt-ins | 958 | 957.84 | 191.57 | PASS |
| SMS Opt-Out Date Exists | 252 | 251.70 | 50.34 | PASS |
| VOD Optouts | 11654 | 11653.56 | 2330.71 | PASS |
| Customers with VOD Viewed | 9056 | 9056.24 | 1811.25 | PASS |
| CDP identity | 46500 | 46500.00 | 9300.00 | PASS |
| UID only | 3500 | 3500.00 | 700.00 | PASS |
| Known to Zeta | 24500 | 24500.00 | 4900.00 | PASS |
| No active phone | 46000 | 46000.00 | 9200.00 | PASS |
| Donor accounts | 15095 | 15050.00 | 3010.00 | PASS |
| 30 day donation | 575 | 574.71 | 114.94 | PASS |
| 30 day donation_return | 5 | 5.17 | 1.03 | PASS |
| 30 day order_shipped | 33 | 32.76 | 6.55 | PASS |
| Synthetic enrichment | 24500 | 24500.00 | 4900.00 | PASS |

## Timings

| Phase | Seconds |
|---|---:|
| generate | 3.065 |
| csv_sql_oracle | 3.646 |
| real_imports | 87.803 |
| identity | 115.726 |
| traits | 10.138 |
| rollups | 3.570 |
| postgres_oracle | 5.368 |
| default_dashboard | 0.038 |

Pytest acceptance: 1 passed in 231.26 seconds. This is the local 50k test, not a production 500k performance claim.

## Independent SQL

### All TBN Reference

```sql
SELECT count(*) FROM audit_people p WHERE master_id IS NOT NULL
```

### Has Active Email

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'has_active_email'='true'
```

### Active (120 days)

```sql
SELECT count(*) FROM audit_people p WHERE NULLIF(attrs->>'last_seen','')::timestamptz>=:active OR NULLIF(attrs->>'last_contact','')::timestamptz>=:active
```

### TBN Plus Subscription

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_plus_subscription' IN ('basic','premium_monthly','premium_annual')
```

### TBN Plus Free Users

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_plus_subscription'='basic'
```

### TBN Plus Premium

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_plus_subscription' IN ('premium_monthly','premium_annual')
```

### Premium Upgraders

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_plus_subscription_update' LIKE 'Upgrade%'
```

### TBN App Non-Donors

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_plus_subscription' IN ('basic','premium_monthly','premium_annual') AND NOT (NULLIF(attrs->>'donor_account_number','') IS NOT NULL) AND NOT (p.id IN (SELECT profile_id FROM events WHERE name='donation' AND occurred_at>=:app AND occurred_at<:as_of))
```

### TBN App Donors

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_plus_subscription' IN ('basic','premium_monthly','premium_annual') AND p.id IN (SELECT profile_id FROM events WHERE name='donation' AND occurred_at>=:app AND occurred_at<:as_of)
```

### Basic Donors

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_plus_subscription'='basic' AND NULLIF(attrs->>'donor_account_number','') IS NOT NULL AND NULLIF(attrs->>'lifetime_amount','')::numeric>0
```

### Basic Recurring Donors

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_plus_subscription'='basic' AND attrs->>'has_recurring_donation'='true'
```

### Basic One-Time Donors

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_plus_subscription'='basic' AND NULLIF(attrs->>'rfm_raw_frequency','')::numeric=1
```

### RFM TBN All Donor Reference

```sql
SELECT count(*) FROM audit_people p WHERE NULLIF(attrs->>'donor_account_status','') IS NOT NULL AND NULLIF(attrs->>'rfm_date','') IS NOT NULL
```

### RFM Lapsed Donors

```sql
SELECT count(*) FROM audit_people p WHERE NULLIF(attrs->>'rfm_recency_score','')::numeric=0
```

### Partner Names Non-Donors

```sql
SELECT count(*) FROM audit_people p WHERE NULLIF(attrs->>'donor_account_number','') IS NOT NULL AND NOT (NULLIF(attrs->>'rfm_date','') IS NOT NULL)
```

### Donors Past 30 Days

```sql
SELECT count(*) FROM audit_people p WHERE p.id IN (SELECT profile_id FROM events WHERE name='donation' AND occurred_at>=:recent AND occurred_at<:as_of)
```

### Living Legacy Prospects V2

```sql
SELECT count(*) FROM audit_people p WHERE NULLIF(attrs->>'donation_likelihood_prob','')::numeric>.75 AND NULLIF(attrs->>'wealth_score','')::numeric>4 AND NULLIF(attrs->>'amt_suggested','')::numeric>500 AND p.id IN (SELECT profile_id FROM events WHERE name='donation' AND occurred_at>=:start AND occurred_at<:as_of)
```

### Giving Tuesday 2024

```sql
SELECT count(*) FROM audit_people p WHERE p.id IN (SELECT profile_id FROM events WHERE name='donation' AND properties->>'source_code' IN ('T4XWDGT','T4XPCGT','T4YWDGT','T4YPCGT'))
```

### Mobile Subscribed

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'mobile_mktg_optin_tbn'='true' AND phone IS NOT NULL
```

### Call Center SMS Opt-ins

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_call_center_sms_optin'='true' AND phone IS NOT NULL
```

### SMS Opt-Out Date Exists

```sql
SELECT count(*) FROM audit_people p WHERE NULLIF(attrs->>'mobile_mktg_optout_date_tbn','') IS NOT NULL
```

### VOD Optouts

```sql
SELECT count(*) FROM audit_people p WHERE attrs->>'tbn_video_on_demand_updates_opt_status'='false'
```

### Customers with VOD Viewed

```sql
SELECT count(*) FROM audit_people p WHERE p.id IN (SELECT profile_id FROM events WHERE name='vod_viewed')
```

