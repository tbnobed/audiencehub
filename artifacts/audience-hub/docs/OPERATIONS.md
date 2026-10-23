# First-party identity graph

For the TBN+ Okta → FusionAuth identity mapping, dual-key ingestion and
production verification, see [FUSIONAUTH_MIGRATION.md](FUSIONAUTH_MIGRATION.md).

For Meta Page setup, first-party click/Login capture, Marketing reporting and
the video-privacy restrictions, see [META_CAPTURE.md](META_CAPTURE.md).

See [IDENTITY_GRAPH.md](IDENTITY_GRAPH.md) for the disabled-by-default scheduled
Splink matcher, evidence/unmerge rules, address/vendor adapters, household
exports, authenticated device signing and server verification checklist.

# M8 operations runbook

## Release status and first upgrade

M8 is a code-only delivery. No tests, application starts, database migrations,
imports, seeds, audits, load tests or benchmarks were run in Replit. Lockfile
resolution and source syntax checks are not runtime acceptance.

This release changes authentication to **local accounts only**. Before restarting,
set `AUTH_MODE=local` in the server configuration. OIDC is disabled, not silently
used as a fallback. Existing users retain IDs and ownership; credentials live in
separate tables so a later external-provider adapter need not recreate users.
Migration `0041_m8_operations` requires case-insensitively unique user emails.
Resolve duplicate accounts deliberately before upgrading; it does not merge users.

Back up PostgreSQL, the appdata volume, and the matching secret configuration.
Do not place those secret backups in Git, shell history, benchmark results or logs.
Then, from `artifacts/audience-hub` on the production server:

```sh
git pull --ff-only
docker compose build api
docker compose up -d --force-recreate api worker
docker compose exec api kinship local-admin --email ADMIN_EMAIL --reset-password
```

The last command prompts privately for a password and confirmation. For a brand-new
account omit `--reset-password`. Recovery of a lost authenticator additionally
requires explicit `--reset-totp`. Password reset revokes sessions and pending
account links. No password appears in process arguments. The API startup applies
the normal migration chain before serving requests.

## Clean-server installation

1. Install Docker Engine and Compose on a supported Linux host. Configure DNS and
   an HTTPS reverse proxy to the localhost API bind address; do not expose Postgres.
2. Clone the repository, enter `artifacts/audience-hub`, and provision the values
   from `.env.example` through your server secret-management process. Use independent
   high-entropy `SECRET_KEY`, `FERNET_KEY` and `PII_HASH_PEPPER`. Keep them stable
   across API/worker restarts. All services must use the same key configuration.
3. Set `APP_ENV=production`, `AUTH_MODE=local`, the real `PUBLIC_BASE_URL`, database
   credentials, backup mount, and disk-appropriate PostgreSQL memory settings.
   The included Compose memory tuning targets the larger production machine;
   reduce it before using a small VM.
4. Run `docker compose build api` then `docker compose up -d`.
5. Bootstrap the first account with interactive `kinship local-admin` as above.
   Sign in over HTTPS, enroll TOTP under Account security, and invite a second
   administrator before enabling the admin-role TOTP requirement.
6. Configure real sources/destinations. Do **not** seed a go-live database.
7. Check `/healthz`, `/readyz`, jobs, a least-privilege login, and a backup restore
   on the server. Execute the regression suite there before accepting the release.

## Accounts and security policy

Admin → Users supports invite, role changes, enable/disable, unlock, password
reset and explicit authenticator reset. Invites expire in 48 hours; reset links
expire in one hour. When SendGrid is enabled, links are emailed through the worker;
otherwise links are displayed once for secure out-of-band delivery. Tokens are
single use and replaced when another link is issued. Authentication stores token
hashes only; the email outbox temporarily stores Fernet-encrypted message bodies,
erasing them on provider acceptance or expiration. Tokens and links never enter
the jobs payload or audit log.

The browser sign-in form requests only email and password. After a correct
password, users with TOTP enabled receive a separate verification popup accepting
an authenticator or recovery code. Users without TOTP sign in directly (the
existing enrollment gate still applies if the admin-role policy requires TOTP).
The password step creates no authenticated session for an enrolled user.
Popup challenges expire after five minutes, are bound to the signed browser
session, and are single use; cancelling or changing credentials invalidates them.
Failed second factors use the same account/address lockout as password failures.
The existing combined email/password/`totp` CLI request remains supported.
Migration `0043_login_challenges` is required for this sign-in flow.

Passwords must contain at least 14 characters and pass the bundled
`app/auth/breached_passwords.txt` denylist check. The bundled list is a small
offline common-breach denylist, not a claim to contain every breached password;
extend it as part of normal security maintenance. Passwords use salted scrypt.
Ten consecutive account failures cause a 15-minute cooldown. An admin can unlock
the account; a separate address-level cooldown still limits anonymous spraying.
TOTP accepts six-digit RFC 6238 codes with a ±30-second clock window, rejects
replayed codes and encrypts enrollment secrets with Fernet. Any standard TOTP
authenticator works: SHA-1, six digits, 30-second period (Google/Microsoft
Authenticator, Authy, 1Password, Bitwarden, etc.). QR codes are generated locally;
no QR service receives the shared secret. Enrollment expires after ten minutes.
Keep server and phone clocks synchronized. After confirmation, save the ten
one-time recovery codes offline; each replaces an authenticator code once.
Only hashes are stored. Account security can regenerate codes after password and
second-factor verification, invalidating every old code. An explicit admin or
server-console TOTP reset also invalidates codes. Neither secrets nor recovery
codes are emailed. If all codes/devices are lost, use a second administrator or
the audited server-console recovery command.

An administrator can require TOTP for all admins only after enrolling their own
authenticator. An unenrolled admin may sign in only to enroll; partner data and
admin APIs remain inaccessible. All local sessions are server-backed, expire
after eight hours idle, and are revoked on password change/reset or account/role
changes. Browser polling is not treated as ongoing human activity.

Login success/failure, lockout, password/TOTP changes, invitations, account changes
and role changes are audited. Admin → Audit log filters by actor, exact action,
object type or `type:id`, and inclusive UTC date range. CSV exports are themselves
audited, spreadsheet-formula escaped, and capped at 100,000 rows; narrow filters
instead of accepting silent truncation.

### SendGrid account email relay

Upgrade the API image and run migrations normally (`0042_account_delivery`),
then open **Admin → Users → Account email relay — SendGrid**:

1. Verify the sending domain/sender in your own SendGrid account (SPF/DKIM;
   configure DMARC according to your domain policy).
2. Create a restricted SendGrid API key with **Mail Send** permission. Enter it
   only in the password-masked relay form, never in chat, logs or CLI arguments.
   Kinship encrypts it with the existing Fernet key and never returns it to the
   browser. A blank key when saving keeps the current key.
3. Set the verified sender address/name and your external HTTPS Kinship URL, e.g.
   `https://cdp.obtv.io`. The configured URL, not the request Host header, is used
   for account links. Enable and save the relay.
4. Send a test to an address you control. Both API and worker need the same
   database and Fernet configuration. The worker needs outbound HTTPS to
   `api.sendgrid.com:443`; this implementation uses the fixed public v3 Mail Send
   API directly, not an SMTP server, proxy, Replit service or SendGrid SDK.
5. Check recent email delivery history. **Queued** is not sent; **Accepted by
   SendGrid** means HTTP 202, not inbox delivery. Check provider Email Activity
   for bounces/suppression and sender issues.

Invites and admin password resets automatically queue email when enabled. There
is no public, unauthenticated password-reset endpoint; an administrator issues
resets. An enrolled authenticator remains required after a password-only reset.
Use “Also reset TOTP” only for intentional, audited second-factor recovery.

The durable worker retries failures five times with its queue backoff, preserving
the same single-use link. Delivery history exposes safe error summaries and a
manual Retry for failed, unexpired messages. Reissuing a reset cancels older
tokens; the worker checks validity and account status before sending. SendGrid
does not guarantee idempotency: a network disconnect after acceptance can cause
a duplicate email containing the same single-use link. Click/open tracking is
disabled for these security messages. No provider response body or bearer
credential is stored in history. Daily maintenance clears expired message
ciphertext and removes cleared receipts after 90 days.

Rotate SendGrid credentials by replacing the API key in this form, sending a test,
then revoking the old key in SendGrid. Disabling the relay prevents sending queued
messages (they retry/fail), and new account links use explicit manual sharing.

## Reset, seed and go-live wipe guards

These commands are destructive or synthetic. They are not routine upgrades.
Production commands require both an explicit production authorization flag and
the **exact connected database name**, not a server hostname or URL.

```sh
# Only for an intentional production demo reset; deletes destinations too:
docker compose run --rm --no-deps api kinship reset-demo \
  --yes --force --confirm-database audience_hub

# Only for an intentional demo load; dates default to today UTC:
docker compose exec -T api kinship seed --profiles 500000 --load \
  --allow-production --confirm-database audience_hub

# For a deliberate go-live transition after taking a verified backup:
docker compose stop api worker
docker compose run --rm --no-deps api kinship wipe-demo \
  --yes --allow-production --confirm-database audience_hub
docker compose start api worker
```

Do not execute the reset/seed examples against real partner data.
`wipe-demo` preserves users/credentials/policy, sources and encrypted connections,
destinations, settings, audit history and the permanent Master ID registry.
Destination configurations/secrets are retained but their delivery switches are
paused, preventing an empty managed-list sync during the transition. Re-enable
destinations deliberately after loading and validating the real consented data.
It removes demo dataset rows and activations and rebuilds the audited audience
definitions. It refuses mixed/unproven contact or gift provenance, non-synthetic
events, active orphan profiles, running jobs and busy tables. It is deliberately
not a selective real-data cleanup tool. Source cursors/recovery state are cleared.
Registered import/error/export files are removed through durable cleanup receipts;
if interrupted after the database commit, daily maintenance retries cleanup.
Unregistered original generator output and operator backups are outside that
allowlist: archive or remove those deliberately under the server storage policy.
Do not reuse old exported files for delivery.

History is generated for the five years ending at UTC `as_of`, excluding today.
Use `--as-of YYYY-MM-DD` only for reproducibility. Dashboard defaults remain rolling,
not anchored to maximum gift dates or a fixed 2020–2024 interval.

## Maintenance and history

Admin → Maintenance exposes retention policy, manual enqueue and recent run
history. The durable daily scheduler enqueues `maintenance.run`; committed batch
work is idempotent and can resume after interruption. Worker jobs retain phase,
row counts and final totals. API/worker must be on the migrated version.

- Event retention defaults to 730 days. It deletes nonfinancial event facts only.
  Donation and donation-return evidence and the gift ledger are not expired.
  Retention therefore deliberately changes historical nonfinancial audience facts.
- Audit retention defaults to **disabled** (0); enabling it discards old audit
  records, including security history. Choose according to TBN's policy.
- Terminal job retention defaults to 90 days. Jobs referenced by imports or durable
  scheduler receipts are retained rather than breaking history/FKs.
- Enrichment is removed when its value/source license expires. Optional age-based
  retention defaults to disabled (0). Expired keys are removed from retained
  source evidence and affected traits are marked dirty to prevent resurrection.
- Uploads/exports use the existing configured retention and privacy cleanup.
  Suppression/deletion tombstones, identity keys, gift history and users are never
  erased by these general retention settings.
- Current month plus three future event months are created by default
  (configurable 1–24 months ahead). If rows already landed in `events_default`,
  they are moved and attached atomically. Partition creation briefly requires an
  exclusive events lock and fails/retries rather than hanging behind active I/O.

Set retention days to 0 to disable age-based deletion for that category.
Expiration of a licensed enrichment value is mandatory even when age-based
retention is disabled. Review policy before the first worker starts.

## Backup and restore

The backup container takes `pg_dump -Fc` backups and verifies their catalog.
Catalog verification is not proof of restoration. Back up appdata as well:
uploads, staged migration manifests, activation exports and encryption-dependent
artifacts must be restored consistently with the database. Back up secret
configuration separately under restricted access, including all retained keys.

Monthly scratch restore, using a new empty database name:

```sh
docker compose exec -T db createdb -U audience audience_restore_check
docker compose exec -T db pg_restore -U audience -d audience_restore_check \
  --no-owner --exit-on-error < /PATH/TO/VERIFIED.dump
docker compose exec -T db psql -U audience -d audience_restore_check \
  -c "SELECT count(*) FROM profiles;"
# Inspect schema/migration revision, key tables, constraints and selected facts.
# Drop the scratch database only after review.
```

Disaster recovery **replaces the target database contents**. Stop API, worker and
backup writer, save a fresh recovery snapshot, confirm the target database name,
and restrict network access. Restore to a new database and point the application
at it whenever possible. If replacing the existing empty/recreated target:

```sh
docker compose stop api worker backup
docker compose exec -T db pg_restore -U audience -d audience_hub \
  --no-owner --exit-on-error < /PATH/TO/VERIFIED.dump
```

The target in that example must already be empty. Do not apply it over live data.
Restore the matching appdata and secret set; then start the same application
revision first, review migration compatibility and only then upgrade. Validate
suppression/deletion tombstones and consent before resuming outbound deliveries.

## Fernet rotation

Never replace `FERNET_KEY` alone. Take verified backups and stop API/worker and
CLI pulls. Configure the new `FERNET_KEY` and place every old read key in the
comma-separated `FERNET_KEYS_PREVIOUS` through the server secret configuration.
Do not print keys, put them on CLI arguments, or commit them.

```sh
docker compose stop api worker
docker compose run --rm --no-deps api kinship rotate-fernet --yes
docker compose up -d --force-recreate api worker
```

The command is transactional and safe to rerun. It rewraps source/destination
credentials, activation snapshots, pending privacy identifiers, active/pending
TOTP secrets, the SendGrid API key and pending encrypted account emails.
Unknown/missing keys roll the transaction back. Current
keys write; the keyring can still read older ciphertext during transition.
External encrypted `identity-filedrop` configuration files and older backups are
outside this database command: rewrap those in the secured maintenance environment
or retain their old keys. Do not retire an old read key until all such artifacts
are accounted for. Keep keys for retained backups in offline escrow.

## PII pepper rotation

Suppression hashes are deliberately one-way, including identifiers whose cleartext
was deleted. There is no safe blanket SQL "rehash" without the original values.
Rotation therefore uses a retained read keyring rather than dropping protection.

1. Back up and stop writers. Keep the current pepper securely.
2. Set a new `PII_HASH_PEPPER`; put the old pepper and every older still-needed
   pepper in comma-separated `PII_HASH_PEPPERS_PREVIOUS`. All workers/API/CLI
   processes must receive the same keyring before restart.
3. Recreate the API/worker containers. New tombstones use the new pepper; import,
   event ingestion, manual opt-in checks, activation exports and HubSpot lists
   check every retained pepper. Pending deletion requests still verify old hashes.
4. On the server, verify re-import and every export path against known old
   tombstones before resuming delivery.
5. **Do not remove an old pepper while tombstones generated with it remain.**
   A lost original cannot be rehashed. Old pepper retention is necessary for
   permanent suppression; rotation does not erase that requirement.

Changing `SECRET_KEY` invalidates browser cookies separately. It does not rotate
Fernet, rehash passwords or migrate suppression hashes.

## Interrupted jobs

Never truncate or mark all jobs successful to hide an interruption. Inspect System
and the relevant Sources/Imports/Activation/Maintenance history. After an unclean
stop, let stale ownership recover normally or invoke:

```sh
docker compose exec -T api kinship jobs cancel --all-stale
docker compose exec -T api kinship load-status --wait --timeout 14400
```

The first command applies the existing stale-job recovery policy, including owned
SQL-session cancellation. It is not a universal force-cancel. Retrying source
pulls resumes committed cursors/receipts. Never discard already committed import
batches. For maintenance, rerun through Admin; completed delete batches stay
committed. For outbound partial failures review run history before retrying:
remote side effects cannot be rolled back by a PostgreSQL rollback.

## Real connections

- **Snowflake:** Sources → Snowflake connection. Configure account, warehouse,
  database/schema and a least-privilege role; provide the matching private key
  and optional key passphrase. Set approved views/queries and canonical mappings,
  preview 100 rows, test, run, then enable schedule. See
  [SOURCE_MIGRATION_WORKFLOWS.md](SOURCE_MIGRATION_WORKFLOWS.md).
- **HubSpot:** Sources/Destinations → HubSpot. Save the private app token once,
  test account/scopes, choose properties/mapping, and enable subscription polling
  before exports. Outbound sync must use a dedicated static list; it removes
  members outside the consented Kinship segment, including manual additions.
  Review [HUBSPOT.md](HUBSPOT.md), including unchanged-contact unsubscribe tests.
- **SFTP:** Pin the expected SHA-256 server host key obtained out of band. Use a
  dedicated account/key and permitted input/output directories. Credentials are
  encrypted; do not disable host-key verification. Test source mappings and a
  small destination run before scheduling. See [ZETA_ALIGNMENT.md](ZETA_ALIGNMENT.md).

```sh
docker compose exec -T api kinship sources test SOURCE_ID
docker compose exec -T api kinship sources preview SOURCE_ID MAPPING_ID
docker compose exec -T api kinship sources run SOURCE_ID MAPPING_ID
docker compose exec -T api kinship destinations run DESTINATION_ID
```

The final command is the HubSpot managed-list CLI; SFTP runs use the existing
Activation wizard/run history, not that HubSpot-only command.

## Server-only audit and load test

No audit runs automatically in Replit or production. The release image packages
pip-audit, npm and the standalone lockfile:

```sh
docker compose exec -T api kinship security-audit --output-dir /data/benchmarks
```

Both tools run even if one fails. JSON reports and a summary are timestamped.
npm fails the command on high/critical findings; Python fails on *any* advisory
(unknown severity is not assumed safe). Network/tool failures fail closed.
Pinned request-stack/Authlib/cryptography/multipart/Router/Vite dependencies were
updated from obsolete versions, but no claim of a clean audit is made here.

For the workload, create a dedicated event source/write key. Use an analyst test
account and a representative segment; set `KINSHIP_LOADTEST_WRITE_KEY`,
`KINSHIP_LOADTEST_EMAIL`, `KINSHIP_LOADTEST_PASSWORD` and, if enrolled, a fresh
`KINSHIP_LOADTEST_TOTP` in the server operator environment. Pass them to the
container with Compose `exec -e` using names only, not secret CLI values:

```sh
docker compose exec -T \
  -e KINSHIP_LOADTEST_WRITE_KEY -e KINSHIP_LOADTEST_EMAIL \
  -e KINSHIP_LOADTEST_PASSWORD -e KINSHIP_LOADTEST_TOTP \
  api python /app/scripts/loadtest.py \
  --base-url https://YOUR_HOST --segment-id SEGMENT_ID \
  --requests 100 --batch-size 100 --concurrency 4 --acknowledge-writes
```

The workload writes synthetic, run-tagged **nonfinancial events**, so it requires
explicit acknowledgement. Do not use a real partner ID. Results go to
`/data/benchmarks/loadtest-UUID.json` with success/error status counts, accepted
ingestion throughput and preview first/p50/p95 latency. HTTP acceptance is not
identity-resolution completion; first request is not a cold-cache guarantee.
Review generated data under the dedicated source before deleting it. Do not
infer an 8M acceptance or sustained ingestion SLA from a short HTTP test.

## Production acceptance checklist

Run on the server against a disposable database/restore, never live partner rows:

```sh
# The container includes the source tree. Install pytest in a separate verification
# image/environment if it is not present; it is intentionally not a runtime dep.
python -m pytest backend/tests
```

Invoke that command from the artifact root with the dedicated test database and
the project's test environment configured; do not point `DATABASE_URL` at the
production database. Tests in `backend/tests/test_m8_operations_pg.py` cover the
password floor/denylist, TOTP vector/replay, lockout/unlock, session expiry and
password-change revocation, invite single use, last-admin guard, audit filters/CSV,
production database-name guard, retained suppression peppers, Fernet transition,
and empty-database retention SQL. They have not been executed in Replit.

In addition, verify through the deployed UI: invite → acceptance → login; every
role's permitted/forbidden routes; mandatory admin enrollment; a second browser's
session invalidation on reset/disable; CSV filter parity; maintenance queue/run
history; and key-rotation rollback with restored backups. Exercise wipe refusal
on mixed evidence and a successful wipe on a disposable synthetic restore.
Check preservation of users, sources/connections, destinations, settings and
Master ID registry, plus removal of registered local files.

Finally run the dependency audit and the bounded load script above on the server.
Accept only the server's actual outputs; no pass counts or throughput claims are
implied by this code-only delivery.
