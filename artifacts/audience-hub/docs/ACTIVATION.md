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
