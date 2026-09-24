# API

Two surfaces:
- `/api/*` for the UI, authenticated by session cookie (OIDC login), role-checked per route.
- `/v1/*` for event ingestion, authenticated by a per-source write key.

JSON everywhere. Errors use `{"error": {"code": "string", "message": "string", "details": {...}}}` with appropriate HTTP status. List endpoints use `?limit=&cursor=` keyset pagination and return `{"items": [...], "next_cursor": "..."}`. FastAPI's OpenAPI docs are served at `/api/docs` for admins only.

## Auth

| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/auth/login` | public | Redirect to OIDC provider (or dev role picker when `AUTH_MODE=dev`) |
| GET | `/auth/callback` | public | Completes OIDC, creates or updates `users`, sets session cookie |
| POST | `/auth/logout` | any | |
| GET | `/api/me` | any | `{id, email, name, role}` |

## Sources and imports

| Method | Path | Role |
|---|---|---|
| GET / POST | `/api/sources` | viewer / admin |
| GET / PATCH | `/api/sources/{id}` | viewer / admin |
| POST | `/api/sources/{id}/rotate-key` | admin (returns the new write key once) |
| POST | `/api/imports` (multipart: `source_id`, `record_type`, `file`) | analyst |
| GET | `/api/imports/{id}/preview` | analyst — first 50 rows, detected headers, suggested mapping |
| PUT | `/api/imports/{id}/mapping` | analyst — `{ "columns": { "Email Address": "email", "Gift Amt": "amount", "HH Income": {"enrichment": "hh_income_band", "data_type": "enum"} }, "options": {"date_format": "MM/DD/YYYY", "phone_region": "US"} }` |
| POST | `/api/imports/{id}/validate` | analyst — dry run on first 5,000 rows, returns error summary |
| POST | `/api/imports/{id}/run` | analyst — enqueues `import.run` |
| GET | `/api/imports`, `/api/imports/{id}` | analyst — status, progress, counts |
| GET | `/api/imports/{id}/errors.csv` | analyst |

Mapping targets per record type:
- `contact`: `external_id, email, phone, first_name, last_name, address1, address2, city, region, postal_code, country`, plus any column → `attributes.<key>`.
- `gift`: `external_id (gift id), contact_external_id, email, phone, amount, gift_date, fund, campaign, appeal_code, channel, payment_method, is_recurring, recurring_plan_id`. A gift attaches to a profile via `contact_external_id` in the same source, else email or phone.
- `event`: `contact_external_id, email, phone, name, occurred_at, properties.<key>`.
- `enrichment`: one identifier column (`contact_external_id`, `email`, or `phone`) plus any number of `enrichment` attribute columns with a declared data type.
- `consent`: identifier column, `channel`, `status`, `captured_at`.

Header auto-suggest matches common variants case-insensitively (`E-mail`, `Email Address`, `Primary Email` → `email`; `Amount`, `Gift Amount`, `Gift Amt` → `amount`; `Date`, `Gift Date`, `Received` → `gift_date`; `Zip`, `ZIP Code`, `Postal` → `postal_code`).

## Profiles

| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/api/profiles?q=&donor_status=&source=&limit=&cursor=` | viewer | Trigram search on name/email/phone. Viewer gets masked email (`j***@gmail.com`) and phone (`***-***-0123`) |
| GET | `/api/profiles/{id}` | viewer | Profile, traits, consents, identifiers, source records summary, enrichment (non-expired), segments. Writes `profile.view` audit entry. Merged IDs redirect: `{"merged_into": 123}` with 301-style handling in the UI |
| GET | `/api/profiles/{id}/gifts` | viewer | Paginated |
| GET | `/api/profiles/{id}/events` | viewer | Paginated, newest first |
| GET | `/api/profiles/{id}/merges` | analyst | |
| PUT | `/api/profiles/{id}/consents/{channel}` | analyst | Manual consent change with a required note |

## Segments

| Method | Path | Role |
|---|---|---|
| GET | `/api/segments/fields` | viewer — registry for the builder |
| GET | `/api/segments/fields/{key}/values?q=` | viewer — typeahead for enum-like fields |
| POST | `/api/segments/preview` | viewer — body is a definition; returns `{count, took_ms}` |
| POST | `/api/segments/sample` | viewer — 25 profiles (masked per role) plus breakdowns |
| GET / POST | `/api/segments` | viewer / analyst |
| GET / PUT / DELETE | `/api/segments/{id}` | viewer / analyst / analyst (archives) |
| POST | `/api/segments/{id}/materialize` | analyst |
| GET | `/api/segments/{id}/counts` | viewer — history |
| GET | `/api/segments/{id}/members?cursor=` | analyst |

## Destinations and activations

| Method | Path | Role |
|---|---|---|
| GET / POST | `/api/destinations` | analyst / admin |
| GET / PATCH / DELETE | `/api/destinations/{id}` | analyst / admin / admin |
| POST | `/api/destinations/{id}/test` | admin — webhook test ping |
| GET / POST | `/api/activations` | analyst |
| GET / PATCH / DELETE | `/api/activations/{id}` | analyst |
| POST | `/api/activations/{id}/run` | analyst |
| GET | `/api/activations/{id}/runs` | analyst |
| GET | `/api/activation-runs/{run_id}/download` | analyst — streams the file, audit logged |

## Dashboards

All accept `?from=&to=` (dates) and return chart-ready arrays. Cache results in-process for 5 minutes keyed by params; invalidate on `traits.recompute` completion.

| Path | Returns |
|---|---|
| `/api/dashboards/overview` | Total active profiles, donors, active donors (12m), giving 12m, avg gift, recurring donors, email-opted-in count, each with prior-period comparison |
| `/api/dashboards/giving` | Monthly giving amount and count, new vs returning donors per month, giving by channel, by fund, top campaigns and appeal codes |
| `/api/dashboards/retention` | Year-over-year donor retention rate, cohort table (first-gift year × subsequent years retained %), donor status distribution over time (from `segment_counts`-style snapshots in `trait_snapshots`, recorded monthly) |
| `/api/dashboards/engagement` | Events per day by source, top event names, viewers who became donors (first event before first gift) |
| `/api/dashboards/sources` | Profiles per source, overlap matrix (profiles in both A and B), identifier coverage (% with email, phone, address) |
| `/api/dashboards/data-health` | Pending resolution count, merges per day, blocklist hits, rejected rows by import, auto-blocklisted identifiers awaiting review, enrichment attributes expiring in 60 days |

Add a `trait_snapshots` table (month, donor_status, count, ltv_sum) populated on the first recompute of each month so retention trends don't need historical recomputation.

## Admin

| Method | Path | Role |
|---|---|---|
| GET / PATCH | `/api/admin/users`, `/api/admin/users/{id}` | admin (role override, deactivate) |
| GET / POST / DELETE | `/api/admin/blocklist` | admin |
| GET / POST | `/api/admin/deletion-requests` | admin |
| POST | `/api/admin/deletion-requests/{id}/approve` | admin (second admin required if `DELETION_TWO_PERSON=true`) |
| GET | `/api/admin/jobs?status=&type=` | admin |
| POST | `/api/admin/jobs/{id}/retry`, `/cancel` | admin |
| GET | `/api/admin/audit?action=&user=&from=&to=` | admin |
| GET | `/api/admin/system` | admin — queue depth, partitions, DB size, last scheduled runs |

## Ingestion (`/v1`)

Authentication: `Authorization: Basic base64(<write_key>:)` (Segment-compatible) or `X-Write-Key` header. Keys are per event source; only the SHA-256 is stored.

| Method | Path | Body |
|---|---|---|
| POST | `/v1/track` | `{ "anonymousId"?, "userId"?, "event": "Video Watched", "properties": {...}, "context": {...}, "timestamp"?, "messageId"? }` |
| POST | `/v1/identify` | `{ "anonymousId"?, "userId"?, "traits": {"email"?, "phone"?, "firstName"?, "lastName"?, ...}, "timestamp"?, "messageId"? }` |
| POST | `/v1/page` | `{ "anonymousId"?, "userId"?, "name"?, "properties": {"url", "path", "title", "referrer"} }` |
| POST | `/v1/batch` | `{ "batch": [ {"type": "track", ...}, {"type": "identify", ...} ] }`, max 500 messages / 1 MB |

Rules:
- One of `anonymousId` or `userId` is required.
- `messageId` defaults to a UUID; duplicates are ignored (idempotent).
- `timestamp` more than 7 days in the future or 2 years in the past is clamped to `received_at` and flagged in `context`.
- Payload limit 32 KB per message; `properties` depth ≤ 5.
- Response `202 {"accepted": n, "rejected": [{"index": i, "reason": "..."}]}`.
- Rate limit per write key: 200 req/s burst, 50 req/s sustained (in-process token bucket; good enough for one API container).
- CORS allowed only for origins in `INGEST_CORS_ORIGINS`.
- IP addresses are truncated to /24 (IPv4) or /48 (IPv6) before storage.

## JavaScript SDK (`/sdk/ah.js`)

Tiny (< 5 KB minified), no dependencies, served by the API with a long cache header and a content hash in the query string.

```html
<script>
  window.ahConfig = { writeKey: "wk_live_xxx", host: "https://audience.obtv.io" };
</script>
<script async src="https://audience.obtv.io/sdk/ah.js"></script>
<script>
  ah.page();
  ah.track("Video Watched", { video_id: "abc", series: "Praise", percent: 75 });
  ah.identify("crm-12345", { email: "donor@example.com" });
</script>
```

Behavior: generates and persists `anonymousId` in a first-party cookie (`ah_aid`, 1 year, `SameSite=Lax`) with `localStorage` fallback; queues calls made before load; batches and flushes every 2 s or 20 messages via `fetch` with `keepalive`, and `sendBeacon` on page hide; respects a global `ah.optOut()` that sets a cookie and stops all sends; does nothing if `navigator.globalPrivacyControl` is true unless the site sets `ahConfig.ignoreGPC` (default false).

Server-side senders (apps, Five9 integration, the streaming platform backend) call `/v1/batch` directly with a server write key.
