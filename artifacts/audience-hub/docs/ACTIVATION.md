# Activation

An activation connects one segment to one destination with a field mapping, an optional consent requirement, and an optional schedule. Every run re-materializes the segment first.

## Selection pipeline (every destination)

1. Materialize the segment.
2. Join `segment_membership` to `active_profiles`.
3. If `required_consent` is set, keep only profiles with `consents.status = 'opted_in'` for that channel. Destinations of type `google_customer_match` and `meta_custom_audience` default `required_consent` to `ads_personalization` with an explicit admin override that is recorded in the audit log.
4. Exclude profiles whose normalized email or phone HMAC matches `suppressions`.
5. Exclude profiles lacking the fields the destination needs (for example no email and no phone for a hashed audience).
6. Record counts at each step on the `activation_runs` row so the UI shows a funnel: selected → consent → suppression → missing fields → sent.

## Destinations

### CSV export
- Config: columns (any profile field, trait, enrichment attribute, or a constant), header labels, delimiter, include/exclude PII toggle.
- Written to `EXPORT_DIR/<run_id>/<segment-slug>-<timestamp>.csv`, streamed, never built in memory.
- Download link requires analyst role; every download writes `export.download` to the audit log.
- Files are deleted after `EXPORT_RETENTION_DAYS`.

### Google Customer Match file
- Columns per Google's customer list template: `Email`, `Phone`, `First Name`, `Last Name`, `Country`, `Zip`.
- Values normalized per Google's rules (lowercase and trim email, E.164 phone, lowercase names without punctuation, 2-letter country, 5-digit US zip), then SHA-256 hex. Country and zip are not hashed.
- Multiple emails or phones per profile: one row per profile using survivor values in the MVP.
- Output CSV ready for manual upload. API sync is a later phase.

### Meta custom audience file
- Columns: `email`, `phone`, `fn`, `ln`, `zip`, `ct`, `st`, `country`, `external_id`.
- Normalize per Meta's rules (lowercase, trim, phone digits only with country code, first name without punctuation, state 2-letter lowercase), then SHA-256 hex. `external_id` is the profile ID, hashed.
- The `external_id` mapping is fixed to `profile.id`; custom or cleared mappings
  are rejected explicitly. Other provider fields remain configurable.
- Output CSV ready for manual upload.

### Webhook
- Config: URL, HTTP method (POST), batch size (default 500), custom headers, HMAC signing secret (encrypted at rest with Fernet), field mapping, mode (`full` sends all current members, `delta` sends `added` and `removed` since the last successful run).
- Payload:
  ```json
  { "activation_id": 3, "run_id": 88, "segment": {"id": 12, "name": "Lapsed LTV 500+"},
    "mode": "delta", "batch": 1, "batches": 4,
    "added": [ {"profile_id": 101, "email": "...", "first_name": "..."} ],
    "removed": [ {"profile_id": 202} ] }
  ```
- Headers: `X-AH-Signature: sha256=<hex HMAC of raw body>`, `X-AH-Timestamp`, `Idempotency-Key: <run_id>-<batch>`.
- Retries: 5 attempts with exponential backoff (2, 4, 8, 16, 32 s) on network errors, 429 and 5xx. 4xx other than 429 fails the run immediately.
- Outbound target must be an explicit URL entered by an admin. Block private and link-local ranges unless the admin ticks "allow internal network" (SSRF guard).

## Shared rules

- Only analysts and admins can run activations; only admins can create or edit destinations.
- Every run writes `activation.run` to the audit log with counts, never raw PII.
- Scheduled activations use cron expressions evaluated in `America/Chicago` by default (configurable per activation) and are enqueued by the scheduler with `scheduled_runs` dedupe.
- Run history page per activation: status, funnel counts, duration, file download (if any), error text, re-run button.

## Required tests

- Hash output matches known vectors for Google and Meta normalization (include tricky cases: uppercase email with spaces, phone with punctuation, names with accents and apostrophes).
- Consent filtering and suppression exclusion counts are correct.
- Webhook signing verifies with the documented algorithm; retry and failure paths behave as specified (use a local test server).
- SSRF guard blocks `127.0.0.1`, `10.0.0.0/8`, `169.254.169.254`, and hostnames resolving to private addresses unless allowed.
- Delta mode sends correct adds and removes across two runs.

## M7 implementation details

- Destinations and activations are exposed under the paths in `API.md`. File
  destinations produce manual-upload files; Google and Meta API synchronization
  is deliberately not implemented.
- A mapping is an ordered object of output column labels to
  `{kind: "profile"|"trait"|"enrichment"|"constant", key?, source?, value?, required?}`.
  The server validates every source field, preserves column order separately from
  PostgreSQL JSONB, and rejects expired/non-exportable enrichment. `include_pii=false`
  excludes contact/enrichment fields and free-form constants. Explicit
  `required:true` makes a missing mapped value count as a missing-field exclusion;
  ad files always require at least a valid email or phone.
- Fernet encrypts signing secrets, custom-header values and queued configuration
  snapshots. Responses expose only a signing-secret suffix and header names.
  URLs cannot contain credentials, fragments or query strings; put credentials in
  encrypted custom headers instead.
- The SSRF check rejects any non-global DNS answer unless an admin explicitly
  allows internal networking. The validated address is pinned to the TCP
  connection, retaining the original TLS SNI/certificate hostname. Proxies and
  redirects are not used. DNS resolution has a five-second bound; HTTP attempts
  have an absolute ten-second deadline, a bounded response read, and an 8 MiB
  payload cap. Unsafe transport headers cannot be overridden.
- “Five attempts” means five requests total, with four intervening waits of
  2, 4, 8 and 16 seconds (32 seconds is the backoff cap, not a sixth attempt).
  Each retry uses identical body bytes and the same run/batch idempotency key.
  A failed run does not advance the last-successful delta membership. Successful
  added records are counted even if a later batch fails. Explicit operator
  re-runs get a new run ID; delivery jobs are single-attempt at the queue level
  because network retries are handled inside the sender.
- The erasure index and planned filename are durable before any export bytes
  are written. Incomplete/failed exports cannot be downloaded. A worker crash
  therefore cannot leave an untraceable partial file containing deleted partner
  data. Privacy changes serialize against selection/delivery/downloads through
  the schema-scoped privacy advisory lock. Selection/delivery also takes the
  nonblocking resolver/import lock before trait and segment locks to keep
  survivor values stable.
- Numeric five-field cron accepts wildcards, ranges, lists and steps. IANA
  timezone defaults to `America/Chicago`. Nonexistent spring-forward wall times
  are skipped; repeated autumn wall-clock minutes have one durable scheduling
  key. Disabling an activation or its snapshotted destination cancels a queued
  delivery before it sends.

### Provider normalization sources

Google's format guidance:
<https://support.google.com/google-ads/answer/10589050?hl=en-GB>.
Meta's customer-list guidance:
<https://developers.facebook.com/docs/marketing-api/audiences/guides/custom-audiences>.

The project emits SHA-256 **hex**. Google email is trimmed/lowercased and dots are
removed from Gmail/Googlemail local parts; phone is E.164 including `+`.
Names are NFC lowercase Unicode letters with punctuation removed: accents are
retained, not transliterated (`Zoë` → `zoë`, `Pérez-O'Neil` → `pérezoneil`).
Google country and US five-digit zip remain unhashed. Meta phone is country-code
digits, state is two-letter lowercase (US full state names are mapped), and
partner ID is hashed for `external_id`. Regression vectors include uppercase
email, punctuated phones, accents and apostrophes.
