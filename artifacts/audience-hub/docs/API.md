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
| POST | `/api/sources/{id}/write-key` | admin — event_api source with event records; generates/rotates key, `{write_key}` once, no-store |
| GET | `/api/sources/{id}/events?limit=50&cursor=` | analyst — payload-free newest-first live tail `{items,next_cursor}`; numeric cursor |
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

Admins rotate an event API source's key with `POST /api/sources/{id}/write-key`,
returning `{write_key}` once with `Cache-Control: no-store`; old keys immediately
stop authenticating. Source JSON exposes only `has_write_key`.
For browser `sendBeacon`, `/v1/batch` also accepts `{write_key, batch}` in its JSON
body; credentials in query strings are not supported. Never put a key in a URL.
Analysts/admins may read `GET /api/sources/{id}/events?cursor=&limit=50`
(`limit` 1–100), returning `{items,next_cursor}` ordered by descending numeric
event ID. The cursor is exclusive. Tail entries contain `id,type,name,profile_id,
occurred_at,received_at,timestamp_clamped`; arbitrary properties, contexts, user
and anonymous identifiers are omitted.

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

Valid duplicate messages count as accepted but create no additional events,
contacts, or jobs. A source-scoped peppered message digest in
`ingestion_receipts` provides retry idempotency across event partitions, missing
timestamps, and write-key rotation. Clients must reuse message IDs when retrying.
Invalid message entries receive indexed rejections; malformed bodies and oversized
requests return 400/413; invalid keys 401, disallowed origins 403, exhausted key
buckets 429. A batch must contain 1–500 entries. User IDs are limited to 200
characters; anonymous/message IDs and event names to 512. Context/traits/properties
must be objects with depth at most five; invalid supplied contact traits are rejected.

Identify observations use existing contact `source_records`, the unchanged
resolver, and existing identifiers. Anonymous identifiers are source-scoped;
user IDs do not implicitly refer to another source's CRM external IDs—include
email/phone to bridge an existing CRM partner. A dedicated `ingestion.resolve`
job waits for the import/identity barrier and backfills anonymous history. Jobs
carry source IDs only. Privacy locks serialize ingestion with deletion; deletion
also erases pending identify observations and unlinked anonymous history and
retains suppression tombstones for their user/anonymous aliases.

The SDK bundle is built to `dist/sdk/ah.js` and served by the API from that
directory (container `/app/static/sdk`). `/sdk/test.html` is development/test only;
its inline script is authorized by a CSP content hash, not a general inline-script
exception. Seed traffic uses real HTTP:
`INGEST_WRITE_KEY=... python -m app.cli seed --profiles 100 --events --events-host http://127.0.0.1:8000`.
Use a configured key rather than placing one on the CLI. `--load` still exercises
the original CSV import path; `--events` is additive and uses generated reserved
domain contacts, stable retry IDs, and page/track/identify/app traffic.

## JavaScript SDK (`/sdk/ah.js`)

Tiny (< 5 KB minified), no dependencies, plain TypeScript compiled separately with Vite. `npm run build:sdk` writes `dist/sdk/ah.js`, `dist/sdk/manifest.json` and `dist/sdk/test.html`; `npm run build` includes that step. The manifest is `{file:"ah.js",sha256:"<actual bundle SHA-256>",url:"/sdk/ah.js?v=<same SHA-256>"}`. Serve it at `/sdk/manifest.json` with `Cache-Control: no-store`; snippet URLs use its actual content hash, not a fixed version. The generated acceptance HTML embeds the same hash. `npm run test:sdk` builds and runs the isolated JavaScript browser harness (no backend required). The API serves the hashed bundle with a long cache header. The Docker build includes all SDK build inputs and copies its outputs to `/app/static/sdk`; fresh development startup builds the SDK before starting FastAPI.

Create a source with `kind: "event_api"` and `record_types: ["event"]`. Source reads expose only `has_write_key`, never the key. In Data Sources → SDK & live events, an admin can generate/rotate a key and copy the once-only snippet. Closing/dismissing it removes the displayed key. Analysts can read the live event tail; viewers cannot. Tail items are `{id,type,name,profile_id,occurred_at,received_at,timestamp_clamped}` and deliberately exclude payloads, identifiers and keys.

The bootstrap **must run before inserting the async script**. Replace the configuration with the real public ingestion host and browser write key. The UI uses the current browser origin in development; production requires an explicitly entered host. Never use a server-only source key on a public page.

```html
<script>
  // Set these to the actual browser source key and deployed ingestion host.
  window.ahConfig = { writeKey: "YOUR_BROWSER_WRITE_KEY", host: "YOUR_PUBLIC_INGESTION_HOST" };
  window.ah = window.ah || { q: [] };
  ["page", "track", "identify", "flush", "optOut"].forEach(function (method) {
    if (!window.ah[method]) window.ah[method] = function () {
      if (method === "optOut") window.ah.q = [["optOut"]];
      else if (window.ah.q.length < 100) window.ah.q.push([method].concat(Array.prototype.slice.call(arguments)));
    };
  });
  ah.page();
  ah.track("Video Watched", { video_id: "abc", series: "Praise", percent: 75 });
  ah.identify("crm-12345", { email: "donor@example.com" });
  // The source settings UI generates a direct URL with the actual build hash.
  // For this portable example, look up the same generated manifest first.
  var host = window.ahConfig.host.replace(/\/$/, "");
  fetch(host + "/sdk/manifest.json", {cache: "no-store", credentials: "omit"})
    .then(function (response) {
      if (!response.ok) throw new Error("SDK manifest unavailable");
      return response.json();
    })
    .then(function (manifest) {
      if (manifest.file !== "ah.js" || !/^[a-f0-9]{64}$/.test(manifest.sha256))
        throw new Error("SDK manifest content hash invalid");
      var script = document.createElement("script");
      script.async = true;
      script.src = host + "/sdk/ah.js?v=" + manifest.sha256;
      document.head.appendChild(script);
    })
    .catch(function (error) { console.error("Audience Hub SDK could not load", error); });
</script>
```

Behavior: generates and persists `anonymousId` in a first-party cookie (`ah_aid`, 1 year, `SameSite=Lax`) with `localStorage` fallback; queues calls made before load; batches and flushes every 2 s or 20 messages via `fetch` with `keepalive`, and `sendBeacon` on page hide; respects a global `ah.optOut()` that sets a cookie and stops all sends; does nothing if `navigator.globalPrivacyControl` is true unless the site sets `ahConfig.ignoreGPC` (default false).

Normal fetch uses `X-Write-Key` with `credentials: "omit"`. Beacon sends `{write_key,batch}` in its JSON **body**, never in the URL (to avoid proxy log leakage). Configure `INGEST_CORS_ORIGINS` for the website origin. Queue capacity is 100 messages; batches are at most 20 messages / 60 KB to fit browser keepalive limits; events over 30 KB or nested properties/traits beyond depth 5 are rejected. Failed network, 429 and 5xx sends retry at most three attempts, keeping the original message IDs and timestamps for server idempotency; other HTTP errors are not retried. Only pending events are beaconed, never the batch owned by an active fetch. `ah.flush()` requests an immediate send. Opt-out persists as `ah_optout=1`, clears pending data and aborts the current client request (data already received by the server cannot be recalled).

Development acceptance: visit `/sdk/test.html`, enter an event source browser write key, load the SDK and send an anonymous page/track event, then identify with a real user external ID. External IDs are source-scoped; include an email to exercise matching an existing CRM profile. Verify the event source tail receives the events and the profile timeline shows earlier anonymous events after resolution/back-fill. The page does not claim success solely because a client call was queued, and does not store the entered key. Existing `ah_optout` or browser GPC prevents sends; manually clear the first-party opt-out cookie and localStorage entry to repeat testing.

Server-side senders (apps, Five9 integration, the streaming platform backend) call `/v1/batch` directly with a server write key.
