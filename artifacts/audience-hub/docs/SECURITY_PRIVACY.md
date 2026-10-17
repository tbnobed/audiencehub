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

### Implemented privacy workflow

- Manual consent changes require an admin and a note of at least 10 characters. Their internal
  authoritative source is inactive and cannot accept CSV imports. The consent
  decision function is shared by ordinary imports, bounded resolution and bulk
  resolution; a newer explicit authoritative opt-in can replace an older opt-out,
  an equal-time opt-out wins, and unknown never clears an existing decision.
- Privacy mutations take an exclusive transaction advisory lock. Imports,
  identity resolution and activation reads/sends take its shared counterpart.
  The key is schema-scoped, so a restricted benchmark schema does not contend
  with production privacy work. Execution defers while an import is running,
  including its validation phase.
- Requests store the submitted identifier only as a suppression HMAC and a
  Fernet-encrypted value. Neither the value nor matching partner IDs is returned
  by the request API. Execution rechecks matches after approval, including new
  matches and merged identities; the encrypted value and matching-ID array are
  cleared after erasure. Related superseded requests are cancelled and cleared.
- Erasure commits the entire matched identity closure atomically, a stronger
  boundary than separate per-partner commits. Schema discovery refuses unknown
  foreign-key or semantic partner references. Known references include dashboard
  partner-day rollups, client diagnostics, nested JSON/job references, merge
  history, dirty traits, and activation membership/snapshot tracking.
- Known external IDs and event message IDs also receive source-namespaced HMAC
  deletion tombstones. Re-imported transactional rows cannot evade deletion by
  omitting their previous email/phone. Truly new records with no surviving known
  identifier cannot be inferred to be the erased person.
- Stored exports and import source files containing erased partners are revoked
  and removed. Revocation failure does not mark the request complete; retry is
  idempotent. Files removed before a later database rollback remain unavailable.
  Copies already downloaded or delivered to an external destination cannot be
  recalled by this application. Backups retain data until their separately
  configured retention expires (`BACKUP_KEEP_DAYS`, default 14).
- Historical uploads are not inferred from `source_records.last_import_id`,
  which changes on re-import. Because older imports have no immutable row-level
  provenance, erasure conservatively revokes **all retained source/error CSVs for
  each affected source**, including earlier versions and files containing other
  partners from that source. Their relational partner records are not deleted.
  Other sources' retained uploads and diagnostics are streamed for known
  normalized email/phone identifiers and mapped external/reference IDs; matching
  files are revoked, unrelated files are retained. Files that cannot be scanned
  when their contents must be checked, cannot be removed, or lie outside the
  upload directory prevent completion rather than being skipped.
  Revoked uploads return 404 from preview/error-download routes.
- The upload-retention helper expires raw and diagnostic files for completed
  imports older than `UPLOAD_RETENTION_DAYS` (default 7), leaving recent or active
  imports untouched. It is designed for the existing scheduled retention
  dispatch; failures leave metadata unchanged and retries are idempotent.
- The deletion-completeness regression queries every physical table in its
  disposable schema, including event partitions, arrays and JSON, for semantic
  references to erased IDs. It also checks encrypted run/request snapshots,
  retained unrelated partition rows, file removal and real contact/gift/event
  re-import rejection. Generic IDs belonging to unrelated entities are not
  mistaken for partner references merely because their numeric values coincide.
- Known email/phone aliases and fully source-qualified external identifiers are
  also redacted from explicitly enumerated user-facing metadata: audience names,
  descriptions and definitions; activation names, mappings and column labels;
  noncredential source/destination configuration; import filenames and mappings;
  audit/job/diagnostic free text; consent notes; event/gift/source attributes;
  enrichment labels/text; and public profile display/address fields. Literal
  boundaries and case-insensitive matching avoid replacing larger unrelated
  identifiers. Formatted canonical phone numbers are supported. This is not
  fuzzy recognition of arbitrary names, unqualified numeric IDs, encoded blobs,
  or identifiers that were never associated with the erased identity.
- Deletion preserves audience status, schedules, unaffected rules, dependencies,
  and activation/destination enabled state. Multi-value `in`/`not_in` rules lose
  only values containing the erased identifier. A scalar or emptied rule becomes
  a fail-closed predicate (`id IS NULL`, or `id IS NOT NULL` inside an odd number
  of negated groups); no redaction placeholder is evaluated as a comparison.
  This also prevents a scalar `neq` or `NOT` rule from becoming match-all.
  Other branches remain usable. Affected audiences and dependents are queued
  for refresh. Removing an exclusion-list value or reducing a referenced audience
  can expand negative audiences; dependencies are deliberately not rewritten or
  frozen. Review these semantics after deletion.
  Each affected audience receives an immutable, identifier-free audit event,
  which doubles as a durable Data Health issue (no new table/migration).
  Analysts/admins can acknowledge individual issues; viewers can only read them.
  New deletions create new issues even after prior review. Description-only
  redaction does not change rules or membership. Historical encrypted snapshots
  and export paths are still checked and affected exports revoked.
- Authentication users/sessions, write-key hashes, credential subtrees (including
  authorization headers), and destination encrypted credentials are deliberately
  excluded. These are account/credential records, not audience metadata. No
  credential values are returned by the scrubber or its verification helper.
- For a previously completed request, the internal
  `privacy.metadata.repair_completed_request(db, request_id, kind, value)` helper
  performs metadata-only repair after checking completion, approval and the
  supplied normalized identifier against the request's stored HMAC. It does not
  recreate a profile or rerun relational deletion. The caller commits, and an
  aggregate-only repair audit is recorded. The read-only
  `metadata_match_counts(db, aliases)` helper verifies this same explicit scope
  and returns only counts; an empty dictionary means no remaining matches.

Privacy/consent/import/identity focused PostgreSQL verification: **107 passed,
1 opt-in test skipped** (35.45 seconds), using a newly initialized disposable
cluster. This is correctness evidence, not a production timing claim. TypeScript
typecheck and Python compilation also passed. No active application data was
used for erasure testing.

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
