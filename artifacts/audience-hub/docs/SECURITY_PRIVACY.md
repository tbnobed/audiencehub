# Security and privacy

This system holds donor PII and giving history. Treat it as the most sensitive dataset in the building.

## Authentication

- Production: OIDC against Authentik (`authlib`), authorization code flow with PKCE, confidential client.
- On callback, verify `iss`, `aud`, `exp`, and nonce. Map group claims to a role: admin > analyst > viewer (highest match wins). A user with none of the groups is denied with a clear message.
- Session: server-signed cookie (`SECRET_KEY`), `HttpOnly`, `Secure` in production, `SameSite=Lax`, 8-hour absolute lifetime, 1-hour idle timeout. The session stores user ID and role; role is re-read from the DB on each request so a deactivated user loses access immediately.
- CSRF: all mutating `/api/*` requests require an `X-CSRF-Token` header matching a token issued in `/api/me`. `/v1/*` is exempt (key-authenticated, no cookies).
- Dev mode (`AUTH_MODE=dev`): login page lets you pick a role and name. The app refuses to start if `AUTH_MODE=dev` and `APP_ENV=production`, and shows an amber `DEV AUTH` badge whenever dev auth is active.
- `app.cli create-admin` exists for bootstrap and break-glass only; it writes to the audit log.

## Authorization

- Route-level role guards (`Depends(require_role("analyst"))`).
- Response-level PII masking for viewers is done in the API serializers, never in the frontend.
- Export, download, and activation endpoints require analyst or admin and always audit.

## Secrets

- All secrets come from environment variables. None are committed. `.env.example` holds placeholders only.
- Destination credentials and webhook signing secrets are encrypted in the DB with Fernet (`FERNET_KEY`). The API never returns them after creation (show `••••` plus last 4).
- Write keys are shown once on creation or rotation and stored as SHA-256.

## Data protection

- Postgres is only reachable on the internal Docker network. No published port.
- Backups are `pg_dump` custom format; store them on an encrypted volume or encrypt them (`age` or `gpg`) before they leave the host.
- Uploaded CSVs are deleted 7 days after a successful import (`UPLOAD_RETENTION_DAYS`, default 7). Export files after `EXPORT_RETENTION_DAYS`.
- IP addresses from events are truncated before storage. User-agent is kept.
- Logs never contain raw emails, phones, names, or addresses. Use profile IDs. Add a logging filter that redacts anything matching an email or phone pattern as a safety net.
- Error reports and import error CSVs are only downloadable by analysts and admins.

## Consent

- Consent is per channel: `email`, `sms`, `phone`, `mail`, `ads_personalization`.
- Sources: consent imports, `identify` traits (`consent_email`, etc.), and manual changes (with a required note).
- Precedence: an opt-out from any source wins over an opt-in with an earlier or equal timestamp. A later explicit opt-in can override an earlier opt-out only if it comes from a source flagged `authoritative_for_consent` (for example the ESP's preference center).
- Activations respect `required_consent`. Ad audiences default to requiring `ads_personalization` unless an admin overrides it for that activation (audited).
- Hard bounces, spam complaints, and manual email suppressions set email consent to `opted_out`. They exclude an address from email-channel activations only; they never reject imported contact, gift, event, or enrichment rows and do not suppress other activation channels.

## Deletion requests (CCPA/CPRA-style right to delete)

1. Admin enters an email or phone. The system normalizes it, finds matching profiles (including merged-away ones), and shows what will be deleted (counts only).
2. Approval (by a second admin when `DELETION_TWO_PERSON=true`).
3. `deletion.execute` job, in one transaction per profile: delete events, gifts, consents, enrichment values, segment memberships, identifiers, source records, and the profile rows. Insert suppression hashes (HMAC-SHA256 with `PII_HASH_PEPPER`) for every email and phone the profiles had.
4. Future imports reject only identifiers with a `deletion_request` suppression. These rows are counted as rejected and reported in the import error file. Hard-bounce, spam-complaint, and manual suppressions do not reject imports; email suppressions with those reasons set email consent to `opted_out` so email-channel activation excludes the address while gifts, events, and enrichment remain importable.
5. Audit log records the request ID, counts, and actor, not the identifier.
6. Note in the admin UI that backups keep deleted data until they age out (`BACKUP_KEEP_DAYS`), and that gift records needed for tax receipting should be handled in the system of record, not here.

## Third-party enrichment licensing

- Each enrichment source has `vendor` and `license_expires_at`.
- `enrichment.expire` deletes values past expiry nightly and emails nothing (no outbound mail in MVP); the Data Health page shows upcoming expiries 60 days out.
- Exports can include enrichment attributes only if the destination config explicitly lists them. Admins can mark an enrichment source `exportable = false`, which blocks its attributes from all exports and activations (only usable inside segment rules).

## Web and API hardening

- Security headers on all responses: `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Strict-Transport-Security` in production. The `/sdk/ah.js` route and `/v1/*` get permissive CORS limited to `INGEST_CORS_ORIGINS`.
- Upload limits: 2 GB per CSV, content sniffed as text, parsed with Python's `csv` module (never evaluated), formula-injection protection on export (prefix cells starting with `=`, `+`, `-`, `@` with a single quote in CSV exports).
- SSRF guard on webhook destinations (see `ACTIVATION.md`).
- Dependency hygiene: pinned versions; `pip-audit` and `npm audit` run in CI (or a make target) and must be clean of high-severity issues before a release.

## Replit development rules

- Only synthetic data from the seed generator in Replit. No real donor, viewer, or giving data ever goes into the Replit workspace, its database, or its secrets.
- No production secrets in Replit. Replit gets its own throwaway `SECRET_KEY`, `FERNET_KEY`, and `PII_HASH_PEPPER`.
- The Replit dev database is disposable. The seed script can wipe and recreate it.
