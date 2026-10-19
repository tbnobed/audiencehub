# Audience Hub

Self-hosted customer data operations console for OBTV/TBN. The console provides login, PostgreSQL job processing, synthetic CSV generation, source management, imports, deterministic identity resolution, profile search/detail, and Data Health. No real donor data is included.

## M5 checks and M7 activation/privacy

M7 is implemented before M6 at the user's request. Activations support CSV,
Google Customer Match and Meta manual-upload files, and signed webhooks. The
wizard configures audiences, mappings, consent, schedules and destinations;
run history includes funnel counts and audited downloads. Admins can manage
consent, suppressions and two-admin deletion requests. Google/Meta API sync is
not included.

Measure the existing application population without database writes:

```sh
docker compose exec api kinship benchmark --segments
```

This reads all 23 code-defined templates in a PostgreSQL-enforced read-only
transaction, reports actual profile counts and first/warm count, preview and
sample timings, and explicitly skips materialization. It never seeds, migrates,
installs templates or writes jobs/audit records. `--profiles` is ignored here.
Use `--segments --isolated --profiles 500000 --scale medium` to retain the
synthetic six-template benchmark, or `--audit-segments --profiles 500000 --scale
medium` for fresh isolated 23-template identity/materialization acceptance.
These explicit isolated modes create and clean up private data.
See [segment benchmarking](docs/SEGMENTS.md#benchmark-modes) for safety and flag
semantics. “First” never means flushed database/OS caches.
See [M5 verification](docs/M5_VERIFICATION.md) for historical independent SQL
plausibility checks on the 50,000-person small seed, and
[M7 verification](docs/M7_VERIFICATION.md) for required-test coverage.
[Earlier-item merge audit](docs/PRE_M7_MERGE_AUDIT.md) records commit IDs and the
remaining uncertainty about the historical production profile crash.

Before updating a deployment, back up and drain workers, apply migrations
through `0028_event_ingestion`, then restart the API and workers together.
Deletion revokes retained exports and historical source files; previously
downloaded/delivered copies and backups cannot be recalled. Review the warnings
in Admin and [Security and privacy](docs/SECURITY_PRIVACY.md) before approval.

## M6 event ingestion and SDK

Event API sources now support rotated write keys, `/v1/track`, `/v1/page`,
`/v1/identify`, and `/v1/batch`. The SDK preserves anonymous history through
identification, respects opt-out/GPC, and uses a content-hashed bundle URL.
Source setup includes a once-only key/snippet and live metadata event tail.
Configure explicit browser origins with `INGEST_CORS_ORIGINS`; production does
not implicitly trust arbitrary origins. See [API and SDK](docs/API.md).
`seed --events` sends through the real HTTP API rather than inserting directly.

Worker shutdown now cancels attempt-owned SQL and retries interrupted work;
Compose allows 30 seconds. Stale recovery terminates owned database sessions
before requeueing. `kinship jobs cancel --all-stale` performs that recovery
manually. See [deployment](docs/DEPLOYMENT.md) for the isolated Docker test.
Deletion preserves running segment/activation configuration and raises audited
Data Health review issues for affected audiences.
See [UI integration evidence](docs/UI_MERGE_VERIFICATION.md) for merged commit
IDs and the completed preview/timeline gaps.
The [combined verification report](docs/M6_AND_FOLLOWUPS_VERIFICATION.md) records
544 passing backend tests, the browser role/SDK checks, bundle sizes, and the
remaining Docker-runtime acceptance limitation.

Bulk-load coordination now waits for the import batch, runs contact identity
once, attaches gifts/events separately, refreshes planner statistics, and then
builds traits/rollups. `kinship load-status --wait` defaults to four hours and
distinguishes a still-running timeout from failure. See
[load monitoring and recovery](docs/IMPORT_RECOVERY.md) and
[full five-source verification](docs/FULL_LOAD_VERIFICATION.md).
The 250k correctness check passed after interruption/recovery; the projected
500k production load is approximately 55–90 minutes, so the under-60-minute
production target is **not yet demonstrated**.

## Development

Requires Python 3.12, Node 20+, npm, PostgreSQL 16, and the variables in `.env.example`. Use a disposable PostgreSQL database with **synthetic data only**. For Replit the preconfigured `DATABASE_URL` is used; do not import real records.

From this directory: `python3 -m pip install ./backend[test]`, `npm ci`, then `bash scripts/dev.sh`. The launcher migrates the database and starts the API on 127.0.0.1:8000, worker, and Vite on port 5000 (or `PORT` if provided by the development preview). Vite proxies `/api`, `/v1`, `/sdk`, and `/auth` to the API. In dev mode choose a role on the sign-in screen; production rejects `AUTH_MODE=dev`.

Check `GET /healthz` and `GET /readyz`. Run backend checks from `backend/` with `pytest -q`; frontend checks here with `npm run build && npm run lint`. Development uploads and exports go into the ignored `.data/` directory; production uses the `/data` volume.

## Synthetic imports (M2)

From this directory, run `UPLOAD_DIR="$PWD/.data/uploads" kinship seed --profiles 50000 --scale small` to generate five CSVs, `ground_truth.json`, and `generator_stats.json` without database credentials or a running database. Files go to `UPLOAD_DIR/seed-data` (default `/data/uploads/seed-data`); the development launcher sets `UPLOAD_DIR` to the writable `.data/uploads` directory for its processes, but a separate shell must set it as shown. Alternatively run `cd backend && UPLOAD_DIR="$PWD/../.data/uploads" python -m app.cli seed --profiles 50000 --scale small`. Use `--output-dir` to choose another location. To import, add `--load` and use the same database, secret and `UPLOAD_DIR` environment as the development server; this enqueues all five files through the same `import.run` worker path as the UI and waits for per-import counts and timing. The stats file compares injected messiness with import warnings, rejections, normalization, deduplication, and error-CSV evidence. `--random-seed` makes generation reproducible; `--scale small|medium|large` changes the event and one-time-gift density. A repeat with identical files is skipped after a completed import. The ground-truth file maps every importable generated record to its true person and marks shared households. Development-generated files under `.data/` are ignored by Git.

In production, use `docker compose exec api python -m app.cli seed --profiles 50000 --scale small --load`; the default `/data/uploads/seed-data` persists on the shared volume. The worker service must be running. The `/sources` page manages source definitions and priority, while `/imports` uploads CSVs and guides mapping, dry-run validation, queued processing, progress, and error CSV downloads. Source deletion is refused if historical imports or records reference it; deactivate instead.

Successful imports enqueue `identity.resolve_batch`. The resolver links source records by normalized strong identifiers, merges existing profiles transitively, applies source-priority survivorship, and keeps blocklisted identifiers out of matching. Profiles can be searched by source and identifier presence, with email and phone masked for viewers. Data Health shows pending resolutions, merges, recent rejected rows, and high-cardinality identifiers awaiting admin review. Shared family contact details can still merge household members; there is no unmerge UI.

## Computed traits (M4)

M4 correctness is signed off; final timing is pending the production-server run (16 cores). Five consecutive pre-M5 full suites each passed 324 tests with one skip. The requested 250k-contact comparison measured medians of 263.692 seconds before write minimization and 267.408 seconds after it, so those optimizations now apply only to incremental resolution. Run `docker compose exec api kinship benchmark --traits --profiles 500000` after rebuilding/updating Compose to measure in an isolated schema in the existing PostgreSQL container, without accessing live application tables. See [M4 verification](docs/M4_VERIFICATION.md) for prerequisites, isolation guarantees, retained evidence and historical timings. Local disposable benchmark data has been cleaned up.

### Consent decisions

Dashboard email consent counts use only `consents`, never email presence or profile
attributes. Contact imports recognize `email_consent` (`opted_in`, `opted_out`,
`unknown`) and optional ISO `consent_captured_at`; the existing
`attributes.email_consent` mapping is also supported. Blank/missing consent does
not manufacture permission; invalid nonblank statuses reject the row. ESP consent
imports use the same status vocabulary with an explicit channel and capture time.
Imported consent is materialized when identity resolution assigns the source record
to a profile. Hard bounces and email suppressions produce email `opted_out`.
An explicit opt-out wins over any opt-in, regardless of timestamp, in imports and
identity merges. Reimporting a newer opt-in cannot reauthorize an opted-out profile.
For other status changes the newer observation wins. These ingestion rules are not
a reconsent authorization mechanism. The channel/status index is migration
`0021_consent_channel_status`, following `0020_dashboard_rollups`.
That deployment migration also backfills resolved historical contact consent
attributes and hard bounces (the previous resolver materialized only
`_import_consent`). It never infers opt-in from email presence, preserves existing
consents except to apply an explicit opt-out, and records source-update time as
the timestamp basis in evidence. Refresh dashboard rollups after migration.

`GET /api/profiles` includes a `traits` object on each profile list item, and accepts `donor_status=prospect|new|active|reactivated|lapsing|lapsed`. `GET /api/profiles/{id}` returns the same computed values under `traits`; `GET /api/traits` returns the trait key, label, type, and description catalog. Trait SQL recomputes with one set-based upsert and a pinned database date. Resolution dirties profiles for an incremental refresh after a ten-minute quiet period; the worker also queues one full refresh nightly. A full refresh records the month's first snapshot and fills up to 24 months of historical `trait_snapshots` from dated gifts. `seed --load` waits for identity resolution, computes traits, and performs the historical snapshot backfill.

For a reproducible acceptance report, run `python scripts/identity_acceptance.py --ground-truth .data/uploads/seed-data/ground_truth.json` from this directory with `DATABASE_URL` pointing to the same **isolated** database used for the seed load (adjust the path if `UPLOAD_DIR` or `--output-dir` differs). It compares ground truth with active resolved profiles and includes import metrics. Use a clean disposable database when measuring accuracy or performance; do not clear a database used by the running application.

To clear **only dataset rows** in a database you explicitly intend to reset, run `cd backend && python -m app.cli reset-demo --yes`. This removes imports (including per-import mappings and batch diagnostics), profiles, records, gifts, events, traits, segment definitions/memberships, destinations, activations/runs, audit log, deletion requests, queued jobs and their scheduled-run markers. It retains only users, sources (including settings/source mappings), enrichment attribute definitions, admin settings and schema migrations. The command prints its exact table scope before execution. `APP_ENV=production` additionally requires `--force --yes`; do not use this on a live deployment. The reset refuses running jobs and busy tables; stop workers and import/API writers before running. It executes a single PostgreSQL transaction without `CASCADE`, and refuses unfamiliar tables until their reset policy is reviewed. Generated CSVs and exported files on disk are **not** removed. Run database tests only in the disposable `python -m app.cli benchmark --imports --test-suite` harness, never against the active database.

## Worker recovery (M1)

`JOB_STALE_SECONDS` defaults to 300. For an isolated, migrated PostgreSQL test database, set it to 2 and run `python scripts/manual_worker_recovery.py --stale-seconds 2 --sleep-seconds 60 --timeout-seconds 90` from this directory. The script starts its own worker, kills it during a synthetic 60-second sleep, starts a new worker, and prints the terminal status and attempt count. Do not run this against a database with another active worker that could claim the synthetic job. The opt-in pytest subprocess case uses `AH_WORKER_RECOVERY_TEST_DATABASE_URL`.

## Production

Use `AUTH_MODE=local`, strong production-only secrets, the database password and public HTTPS URL. Local accounts replace SSO for now. Follow [the M8 operations runbook](docs/OPERATIONS.md) for the clean install, migration, interactive admin bootstrap, account recovery, key rotation, backup restore, real connections and server-only verification. The Compose stack runs Postgres, FastAPI, a worker and daily backups, with API bound to localhost for the TLS reverse proxy.

Seed dates default to today UTC and cover the preceding five years through yesterday; `--as-of YYYY-MM-DD` is available for reproducibility. Dashboard defaults use rolling dates, never the maximum gift date or a fixed seed interval. Configure the date preset under Admin settings. Production seed/reset commands require explicit authorization and the typed connected database name; go-live uses the guarded `wipe-demo` command, not a seed.

The API defaults to `API_BIND_HOST=127.0.0.1`. If a reverse proxy in another container or machine forwards to the server's LAN address, set `API_BIND_HOST` in `.env` to that LAN address (for example `192.168.3.67`) and recreate the API container. Keep the proxy pointed at that address and port `API_PORT`; do not expose the API port to the public internet. Restrict LAN access to the proxy host using your firewall, especially while using the unauthenticated development role picker.

The API serves the built React files. Routes are `/api/*` for the UI, `/auth/*` for login, `/healthz` and `/readyz`. `/v1/*` and `/sdk/*` are reserved for the ingestion milestone. Operational data lives in PostgreSQL; jobs are claimed with `FOR UPDATE SKIP LOCKED`, heartbeated, retried, and requeued after stale heartbeats.

See `docs/MILESTONES.md` for subsequent milestones and `docs/DECISIONS.md` for implementation choices.