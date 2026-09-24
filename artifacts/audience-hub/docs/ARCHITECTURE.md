# Architecture

## Overview

```
                        ┌──────────────────────────── Linux host (Docker Compose) ───────────────────────────┐
 Browser (analysts) ──► │ reverse proxy (existing: NPM/Traefik/Caddy, TLS) ──► api :8000                    │
 Web/app SDK events ──► │                                                       │  FastAPI                    │
                        │                                                       │  - /api/*  (UI API, OIDC)   │
                        │                                                       │  - /v1/*   (ingest, keys)   │
                        │                                                       │  - /sdk/ah.js               │
                        │                                                       │  - /       (built React)    │
                        │                                                       ▼                             │
                        │  worker (python -m app.worker) ◄──── jobs table ──── postgres:16 (volume)           │
                        │   - imports, identity resolution, traits,             ▲                             │
                        │     segment materialization, activations, retention   │                             │
                        │  backup (pg_dump nightly → /backups volume) ──────────┘                             │
                        └─────────────────────────────────────────────────────────────────────────────────────┘
```

Three application containers from one image (`api`, `worker`, and a tiny `backup` container based on `postgres:16`), plus `db`. No other services.

## Components

### API (`backend/app/api`)
- FastAPI, async only where it pays (ingest endpoints). Everything else can be sync route handlers using a SQLAlchemy session per request.
- In production the API also serves the built frontend from `/app/static` with an SPA fallback to `index.html` for non-API paths.
- `/v1/*` ingest endpoints authenticate by write key and do minimal work: validate, write to `raw_events` or `source_records`, enqueue a resolution job, return 202.

### Worker (`backend/app/worker.py`)
- One process, N threads (env `WORKER_CONCURRENCY`, default 4).
- Claims jobs with `SELECT ... FROM jobs WHERE status='queued' AND run_after <= now() ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT 1`.
- Scheduler loop (every 30 s) enqueues due scheduled work: nightly trait recompute, segment refreshes, scheduled activations, enrichment license expiry, raw event retention, backups check. Uses a `scheduled_runs` table with a unique key per (task, window) so two workers never double-enqueue.
- Heartbeats: running jobs update `heartbeat_at` every 15 s. Jobs with a stale heartbeat (> 5 min) are requeued up to `max_attempts`.

### Job types
| Type | Triggered by | Does |
|---|---|---|
| `import.run` | Import wizard | Streams CSV → staging via `COPY`, validates, upserts source records, enqueues `identity.resolve_batch` |
| `identity.resolve_batch` | Imports, ingest | Resolves source records to profiles (see `IDENTITY_RESOLUTION.md`) |
| `traits.recompute` | Nightly, after imports (debounced 10 min) | Set-based SQL recompute of computed traits for all or dirty profiles |
| `segment.materialize` | Segment save, schedule, before activation | Rebuilds `segment_membership`, records count history |
| `activation.run` | Manual, schedule | Builds file or sends webhook, writes `activation_runs` |
| `deletion.execute` | Deletion request approval | Hard delete and suppression (see `SECURITY_PRIVACY.md`) |
| `enrichment.expire` | Nightly | Deletes enrichment values past `license_expires_at` |
| `maintenance.retention` | Nightly | Drops event partitions older than retention, prunes old job rows |

### Database
- PostgreSQL 16, one database `audience_hub`.
- Extensions: `pg_trgm` (profile search), `citext`, `pgcrypto`. Nothing that isn't in the stock `postgres:16` image.
- `events` is range-partitioned by month on `occurred_at`. The worker creates partitions 3 months ahead. This keeps the table portable to ClickHouse later.
- Statement timeout for interactive segment previews: 10 s (`SET LOCAL statement_timeout`).

### Frontend (`frontend/`)
- React + Vite + TypeScript + Tailwind, built into static files that the API serves.
- TanStack Query for server state, TanStack Table for grids, Recharts for charts.
- No runtime calls to anything except the same-origin API.

## Data flow

1. **CSV import:** upload (streamed to `UPLOAD_DIR`, never fully in memory) → mapping saved on the import → `import.run` job → `COPY` into a temp staging table → normalize and validate in SQL/Python batches of 10k → upsert `source_records` and `identifiers_raw` → rejected rows written to an error CSV → enqueue `identity.resolve_batch` → enqueue debounced `traits.recompute`.
2. **Events:** SDK or server → `/v1/track|identify|batch` → insert into `events` (anonymous_id, user_id, traits) → `identify` calls produce a source record for the event source → resolution links `anonymous_id` to a profile, back-filling `events.profile_id` for that anonymous ID.
3. **Segments:** definition JSON → compiler → SQL → count preview (live) or materialize (job) → `segment_membership`.
4. **Activation:** segment membership ∩ consent ∩ not suppressed → formatter → file in `EXPORT_DIR` or webhook POSTs → run record + audit log.

## Configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | `production` enables secure cookies and forbids dev auth |
| `DATABASE_URL` | none | `postgresql+psycopg://user:pass@db:5432/audience_hub` |
| `SECRET_KEY` | none | Session signing, 32+ random bytes |
| `FERNET_KEY` | none | Encrypts destination credentials at rest |
| `PII_HASH_PEPPER` | none | Pepper for suppression hashes; never rotate without a rehash job |
| `AUTH_MODE` | `oidc` | `oidc` or `dev` |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | none | Authentik provider |
| `OIDC_ROLE_CLAIM` | `groups` | Claim holding groups |
| `OIDC_ADMIN_GROUPS`, `OIDC_ANALYST_GROUPS`, `OIDC_VIEWER_GROUPS` | `ah-admins` / `ah-analysts` / `ah-viewers` | Comma-separated |
| `PUBLIC_BASE_URL` | none | Used for OIDC redirect and SDK snippet |
| `UPLOAD_DIR`, `EXPORT_DIR` | `/data/uploads`, `/data/exports` | Docker volume |
| `DEFAULT_PHONE_REGION` | `US` | For phone normalization |
| `GMAIL_STYLE_DOMAINS` | `gmail.com,googlemail.com` | Domains where dots and +tags are stripped from the local part |
| `UPLOAD_RETENTION_DAYS` | `7` | Uploaded CSVs deleted after a successful import |
| `DELETION_TWO_PERSON` | `true` | Deletion requests need a second admin |
| `EVENT_RETENTION_DAYS` | `730` | Raw event retention |
| `EXPORT_RETENTION_DAYS` | `14` | Generated files are deleted after this |
| `WORKER_CONCURRENCY` | `4` | Threads |
| `INGEST_CORS_ORIGINS` | empty | Allowed origins for browser SDK |
| `LOG_LEVEL` | `INFO` | JSON logs to stdout (Graylog via Docker GELF driver if wanted) |

## Observability

- JSON structured logs to stdout with request id, user, job id.
- `/healthz` (process up) and `/readyz` (DB reachable, migrations current).
- Admin "System" page: job queue depth, failed jobs with error text and retry button, last scheduled run per task, DB size, event partition list.

## Future path (not in MVP)

- Move `events` to ClickHouse behind the same `EventStore` interface (keep all event reads going through `app/events/store.py`).
- Add Splink probabilistic matching as a second resolver pass that proposes merges for review.
- Native connectors via dlt pipelines writing to the same `source_records` contract.
