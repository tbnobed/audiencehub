# Data model

All tables use `bigint generated always as identity` primary keys unless noted, `created_at timestamptz default now()`, and `updated_at` where rows change. Money is `numeric(14,2)` in USD. Timestamps are UTC.

## Sources and ingestion

### `sources`
| Column | Type | Notes |
|---|---|---|
| id | bigint | |
| key | text unique | Slug, e.g. `donor_crm`, `web`, `zeta_enrichment` |
| name | text | |
| kind | text | `csv` or `event_api` |
| record_types | text[] | Subset of `contact, gift, event, enrichment, consent` |
| priority | int | Lower number wins in survivorship. Default 100 |
| vendor | text null | For enrichment sources, e.g. `zeta`, `experian` |
| license_expires_at | date null | Enrichment only |
| write_key_hash | text null | SHA-256 of the write key (event sources). Key shown once on creation |
| settings | jsonb | Default mapping, phone region, etc. |
| is_active | bool | |

### `imports`
id, source_id, filename, file_path, file_sha256, record_type, mapping jsonb, status (`uploaded, mapped, validating, running, completed, failed, cancelled`), rows_total, rows_ok, rows_rejected, error_file_path, started_at, finished_at, created_by.

Re-uploading a file with the same `file_sha256` for the same source warns in the UI and requires confirmation.

### `source_records`
One row per external record per source, kept for lineage and survivorship.

| Column | Type | Notes |
|---|---|---|
| id | bigint | |
| source_id | bigint fk | |
| external_id | text | Source's own ID. If the file has none, use a hash of normalized email+phone+name |
| profile_id | bigint fk null | Set by resolution |
| email_norm | citext null | |
| phone_e164 | text null | |
| first_name, last_name | text null | |
| address1, address2, city, region, postal_code, country | text null | |
| attributes | jsonb | Every other mapped column, verbatim |
| raw_hash | text | Hash of the raw row; unchanged re-imports are no-ops |
| last_import_id | bigint | |
| resolved_at | timestamptz null | Null means pending resolution |
| unique (source_id, external_id) | | |

### `jobs`
id, type, payload jsonb, status (`queued, running, succeeded, failed, cancelled`), priority int, run_after timestamptz, attempts int, max_attempts int default 3, heartbeat_at, started_at, finished_at, error text, progress jsonb (`{"done": n, "total": m, "message": "..."}`), dedupe_key text null (partial unique index where status in `queued, running`).

### `scheduled_runs`
task text, window_key text, job_id bigint, created_at. Unique (task, window_key).

## Profiles and identity

### `profiles`
| Column | Type | Notes |
|---|---|---|
| id | bigint | Stable; merged-away profiles keep their row with `merged_into_id` set |
| merged_into_id | bigint null | |
| email | citext null | Survivor value |
| phone | text null | E.164 |
| first_name, last_name | text null | |
| address1, city, region, postal_code, country | text null | |
| first_seen_at, last_seen_at | timestamptz | |
| search_text | text | Lowercased name + email + phone, `pg_trgm` GIN index |
| is_deleted | bool default false | |

Every query that reads profiles filters `merged_into_id is null and not is_deleted`. Put this in a view `active_profiles` and use it everywhere.

### `identifiers`
| Column | Type | Notes |
|---|---|---|
| id | bigint | |
| type | text | `email`, `phone`, `external` (value = `source_key:external_id`), `anonymous_id`, `user_id` |
| value | text | Normalized |
| profile_id | bigint fk | |
| first_seen_at | timestamptz | |
| unique (type, value) | | One identifier belongs to exactly one profile |

### `identifier_blocklist`
type, value, reason. Seeded with junk values (see `IDENTITY_RESOLUTION.md`). Admin-editable.

### `profile_merges`
id, winner_id, loser_id, reason jsonb (which identifier caused it), merged_at, job_id. Append-only.

## Transactions, events, consent

### `gifts`
id, source_id, external_id, profile_id (nullable until resolved), source_record_id, amount numeric, currency text default 'USD', gift_date date, fund text null, campaign text null, appeal_code text null, channel text null (`online, mail, phone, event, other`), payment_method text null, is_recurring bool, recurring_plan_id text null, attributes jsonb. Unique (source_id, external_id). Index (profile_id, gift_date).

### `events` (partitioned by month on `occurred_at`)
id, profile_id null, anonymous_id text null, user_id text null, source_id, type text (`track, identify, page, screen`), name text (e.g. `Video Watched`, `Page Viewed`), properties jsonb, context jsonb (ip truncated to /24, user agent, page url, app version), occurred_at, received_at, message_id text. Unique (source_id, message_id, occurred_at) for idempotency. Indexes: (profile_id, occurred_at desc), (anonymous_id), (name, occurred_at).

All event reads go through `app/events/store.py` so the table can move to ClickHouse later.

### `consents`
profile_id, channel (`email, sms, phone, mail, ads_personalization`), status (`opted_in, opted_out, unknown`), source_id, captured_at, evidence jsonb. Primary key (profile_id, channel). Latest `captured_at` wins, except that an `opted_out` from any source beats an older or equal-time `opted_in`.

### `suppressions`
id, type (`email, phone`), value_hash (HMAC-SHA256 with `PII_HASH_PEPPER` over the normalized value), reason (`deletion_request, hard_bounce, spam_complaint, manual`), created_at. Unique (type, value_hash). Checked on import and on every activation.

## Enrichment

### `enrichment_values`
profile_id, source_id, attribute_key text, value_text text null, value_num numeric null, value_bool bool null, value_date date null, imported_at, license_expires_at date null (copied from source at import time). Primary key (profile_id, source_id, attribute_key).

### `enrichment_attributes`
source_id, key, label, data_type (`text, number, boolean, date, enum`), enum_values text[] null, description. Registered during import mapping. Enrichment attributes appear in the segment builder under their source's vendor name.

When `license_expires_at` passes, `enrichment.expire` deletes the values and marks the attributes inactive. Segments that use them show a warning.

## Computed traits

### `profile_traits`
One row per active profile, recomputed with set-based SQL (`INSERT ... SELECT ... ON CONFLICT DO UPDATE`), never per-profile Python loops.

| Trait | Type | Definition |
|---|---|---|
| gift_count_total | int | All gifts |
| ltv_total | numeric | Sum of all gifts |
| gift_amount_12m | numeric | Sum, last 365 days |
| gift_count_12m | int | Count, last 365 days |
| first_gift_date, last_gift_date | date | |
| largest_gift_amount | numeric | |
| avg_gift_amount | numeric | |
| is_recurring_active | bool | Recurring gift within last 45 days |
| days_since_last_gift | int | |
| donor_status | text | `prospect` (never gave), `new` (first gift within 365 d), `active` (gave in last 365 d, not new), `lapsing` (last gift 366–730 d), `lapsed` (> 730 d), `reactivated` (gave in last 365 d after a gap > 730 d) |
| rfm_recency, rfm_frequency, rfm_monetary | smallint 1–5 | Quintiles over donors only (`ntile(5)`); recency on days since last gift (5 = most recent), frequency on gift_count_total, monetary on ltv_total |
| rfm_score | text | e.g. `545` |
| event_count_30d | int | |
| last_event_at | timestamptz | |
| video_views_30d | int | Events named `Video Watched` |
| last_engagement_channel | text | Source key of most recent event or gift |
| source_keys | text[] | Every source this profile appears in |
| computed_at | timestamptz | |

### `trait_snapshots`
month date, donor_status text, profile_count int, ltv_sum numeric, giving_12m_sum numeric. Primary key (month, donor_status). Written on the first trait recompute of each month so the Retention dashboard can show status trends without historical recomputation. The seed generator back-fills 24 months.

The trait catalog lives in code (`app/traits/registry.py`) with label, type, description, and SQL so the segment builder and profile page can list traits with descriptions. Dates used in "last N days" math come from a single `as_of` parameter so tests can pin time.

## Segments and activation

### `segments`
id, name, description, definition jsonb, status (`draft, active, archived`), refresh_schedule text null (cron expression), last_materialized_at, last_count, created_by, updated_by.

### `segment_membership`
segment_id, profile_id, added_at. Primary key (segment_id, profile_id). Replaced atomically on materialize (build into a temp table, then delete/insert inside one transaction, and record the diff counts).

### `segment_counts`
segment_id, counted_at, count, added, removed. For the count history chart.

### `destinations`
id, name, type (`csv_export, google_customer_match, meta_custom_audience, webhook`), config jsonb (non-secret), secret_encrypted bytea null (Fernet), is_active.

### `activations`
id, segment_id, destination_id, field_mapping jsonb, required_consent text null (channel), schedule text null (cron), is_active, created_by.

### `activation_runs`
id, activation_id, status, started_at, finished_at, profiles_selected, profiles_excluded_consent, profiles_excluded_suppressed, profiles_sent, file_path null, file_sha256 null, error text, triggered_by (`manual, schedule`), user_id null.

## Access and audit

### `users`
id, subject (OIDC `sub`), email, name, role (`admin, analyst, viewer`), last_login_at, is_active. Role is refreshed from group claims on every login.

### `audit_log`
id, at, user_id null, actor_type (`user, system, api_key`), action text (e.g. `profile.view`, `segment.update`, `activation.run`, `export.download`, `deletion.execute`), entity_type, entity_id, details jsonb (no raw PII; use profile IDs and counts), ip. Append-only; the app's DB role has no UPDATE or DELETE grant on this table.

### `deletion_requests`
id, requested_by, identifier_type, identifier_value_hash, status (`pending, approved, executed, rejected`), matched_profile_ids bigint[], executed_at, notes.

## Indexing checklist

- `identifiers (type, value)` unique, `identifiers (profile_id)`.
- `source_records (source_id, external_id)` unique, `(profile_id)`, partial `(id) where resolved_at is null`.
- `gifts (profile_id, gift_date)`, `(gift_date)`.
- `events` per-partition `(profile_id, occurred_at desc)`, `(anonymous_id)`, `(name, occurred_at)`.
- `profiles` GIN trigram on `search_text`, partial index on `(id) where merged_into_id is null and not is_deleted`.
- `profile_traits` btree on `donor_status`, `last_gift_date`, `ltv_total`, `gift_amount_12m`.
- `enrichment_values (source_id, attribute_key, value_text)` and `(source_id, attribute_key, value_num)`.
- `segment_membership (profile_id)`.
