# Deployment (self-hosted Docker)

Production runs on our own Linux server with Docker Engine and the Compose plugin. Replit is only used to develop the code. Nothing in production depends on Replit.

## Host requirements

- Ubuntu 22.04/24.04 or Debian 12, Docker Engine 24+, Compose v2.
- 8 vCPU, 32 GB RAM, 500 GB SSD for the MVP scale targets (Postgres data on SSD).
- Sits behind the existing reverse proxy that terminates TLS (Nginx Proxy Manager, Traefik, or Caddy). Only the proxy is exposed; `api` binds to the internal Docker network or `127.0.0.1`.
- DNS: e.g. `audience.obtv.io` for the UI/API. If the browser SDK is used on public sites, the `/v1` and `/sdk` paths must be reachable publicly; everything else can be restricted to the internal network or VPN at the proxy.

## PostgreSQL lock-table budget

Compose sets `max_locks_per_transaction=1024` as production headroom for concurrent
imports, identity work, migrations, and relation/advisory locks. This sizes a
shared server-wide pool, not a hard per-transaction limit. Capacity scales
approximately with `max_locks_per_transaction * (max_connections +
max_prepared_transactions)`: at 100 connections and no prepared transactions,
1024 budgets about 102,400 lock objects versus 6,400 at the default 64.
Lock and holder bookkeeping use additional shared memory (typically tens of
MiB here; exact allocation depends on PostgreSQL version/build). Budget that
alongside the existing 8 GB shared buffers, connection/work memory, and OS
cache; reconsider the budget when increasing connection limits.

This setting requires a PostgreSQL **restart**, not a reload. After changing the
Compose command, recreate the database container with `docker compose up -d db`
(schedule downtime; keep the persistent volume), then verify with
`SHOW max_locks_per_transaction;`. Raising the setting is defense in depth,
**not a substitute for bounded resolver transactions and advisory-lock buckets**.
The isolated 50,000-record concurrent-import regression deliberately uses
PostgreSQL's default 64 to protect that property.

## Dockerfile (multi-stage, single image)

```dockerfile
# ---- frontend build ----
FROM node:20-bookworm-slim AS web
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.ts tsconfig.json ./
COPY src/ ./src/
COPY public/ ./public/
RUN PORT=5000 BASE_PATH=/ npm run build

# ---- python runtime ----
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 tini curl \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY backend/pyproject.toml ./backend/
COPY backend/app/__init__.py ./backend/app/
RUN pip install --upgrade pip && pip install ./backend
COPY backend/ ./backend/
RUN pip install --no-deps ./backend
COPY --from=web /web/dist/public ./static
COPY scripts/ ./scripts/
RUN chmod +x scripts/*.sh && mkdir -p /data/uploads /data/exports && chown -R app:app /data /app
WORKDIR /app/backend
USER app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/scripts/start-api.sh"]
```

Notes for the implementer:
- `pip install .` must install dependencies from `pyproject.toml`; do not rely on Replit's package manager or `poetry.lock` in the image unless Poetry is installed in the image too. Pin versions.
- Commit `package-lock.json`.
- The image must build on `linux/amd64` without network access to anything except PyPI and npm registries.

## docker-compose.yml

```yaml
name: audience-hub

services:
  db:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_DB: audience_hub
      POSTGRES_USER: ${POSTGRES_USER:-audience}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
    command: >
      postgres -c shared_buffers=8GB -c effective_cache_size=24GB
               -c work_mem=64MB -c maintenance_work_mem=1GB
               -c checkpoint_timeout=15min -c max_wal_size=16GB
               -c wal_compression=on -c random_page_cost=1.1
               -c jit=off -c max_locks_per_transaction=1024
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d audience_hub"]
      interval: 10s
      timeout: 5s
      retries: 10
    networks: [internal]

  api:
    build: .
    image: audience-hub:latest
    restart: unless-stopped
    env_file: .env
    depends_on:
      db: { condition: service_healthy }
    command: ["/app/scripts/start-api.sh"]
    volumes:
      - appdata:/data
    ports:
      - "127.0.0.1:${API_PORT:-8080}:8000"
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/readyz"]
      interval: 15s
      timeout: 5s
      retries: 5
    networks: [internal]

  worker:
    image: audience-hub:latest
    restart: unless-stopped
    env_file: .env
    depends_on:
      db: { condition: service_healthy }
      api: { condition: service_healthy }
    command: ["/app/scripts/start-worker.sh"]
    stop_grace_period: 30s
    volumes:
      - appdata:/data
    networks: [internal]

  backup:
    image: postgres:16
    restart: unless-stopped
    environment:
      PGHOST: db
      PGDATABASE: audience_hub
      PGUSER: ${POSTGRES_USER:-audience}
      PGPASSWORD: ${POSTGRES_PASSWORD}
      BACKUP_KEEP_DAYS: ${BACKUP_KEEP_DAYS:-14}
    volumes:
      - ./scripts/backup.sh:/backup.sh:ro
      - ${BACKUP_DIR:-./backups}:/backups
    entrypoint: ["/bin/bash", "/backup.sh"]
    depends_on:
      db: { condition: service_healthy }
    networks: [internal]

volumes:
  pgdata:
  appdata:

networks:
  internal:
```

If the reverse proxy runs in Docker on the same host, attach `api` to the proxy's network instead of publishing a port.

## Scripts

`scripts/start-api.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${API_WORKERS:-4}" --proxy-headers --forwarded-allow-ips="*"
```
Note: with more than one uvicorn worker the in-process rate limiter and dashboard cache are per-process; that is acceptable for the MVP.

`scripts/start-worker.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
exec python -m app.worker
```

The image defaults to `/app/backend`, including for `docker compose exec api`, so
`python -m app.cli ...` (or the installed `kinship ...` console command) works
without `-w`. Scripts live at `/app/scripts` and are invoked by absolute path.

`WORKER_CONCURRENCY` controls the number of **spawned child processes per worker
container**, not threads or a stack-wide limit. Each child claims at most one
job at a time with PostgreSQL `FOR UPDATE SKIP LOCKED` and maintains its
heartbeat while executing it. To add capacity, use
`docker compose up -d --scale worker=N`; total concurrent jobs can reach
`N * WORKER_CONCURRENCY`. The parent restarts dead children, and a crashed
child's claimed job is recovered after `JOB_STALE_SECONDS` by the existing
stale-job retry policy. Only one worker parent at a time holds the PostgreSQL
session advisory scheduler leader lock. Other replicas continue processing
jobs and can take over scheduling when that connection is released. The
scheduled-run keys protect against duplicate daily/hourly jobs on failover.
During shutdown the parent releases scheduler leadership immediately, then
gives children up to `WORKER_STOP_GRACE_SECONDS` (default/maximum 20 seconds)
to cancel their own SQL, terminate remaining owned sessions, and commit an
immediate retry without consuming an attempt. Compose's `stop_grace_period`
is 30 seconds, including a five-second forced-child reap margin. No new jobs
are claimed after SIGTERM. If cleanup fails or the process is SIGKILLed, its
running row is left for stale recovery rather than unsafely publishing retry.
Never validate recovery against the production database.

Worker helper/heartbeat SQL shares an attempt-specific application name
`kinship-worker-<job-id>-<started-at-digest>`; API sessions use `kinship-api`,
and worker control/scheduler sessions use `kinship-worker-<pid>`.
Recovery serializes against connection registration, terminates only the exact
attempt name in the current database/current role, waits for those backends to
disappear, then requeues (or fails exhausted attempts). Old attempts cannot
heartbeat or finalize replacement attempts. Legacy untagged sessions are never
guessed at or killed; inspect them manually during the first rollout.

`kinship jobs cancel --all-stale` invokes that same recovery using
`JOB_STALE_SECONDS` (default 300). It does not cancel healthy work.
`reset-demo` still refuses running jobs/busy tables and now reports session PID,
application name, query start and a 120-character SQL preview with literals and
comments redacted. Keep this operator output private.

All app connections enable client/server TCP keepalives (30s idle, 10s interval,
3 probes) and `IDLE_TRANSACTION_TIMEOUT_SECONDS` (default 120). These session
settings apply with the app role without requiring superuser ALTER ROLE.
Statement timeout defaults: API/maintenance/noop 60s
(`API_STATEMENT_TIMEOUT_SECONDS`), import/identity/traits 7200s
(`BULK_STATEMENT_TIMEOUT_SECONDS`), segment 600s
(`SEGMENT_STATEMENT_TIMEOUT_SECONDS`), activation and deletion 900s
(`ACTIVATION_STATEMENT_TIMEOUT_SECONDS`, `DELETION_STATEMENT_TIMEOUT_SECONDS`).
All must be positive; narrower existing interactive deadlines remain in place.

Run `python scripts/worker_shutdown_acceptance.py --image audience-hub:latest`
after building the release image to verify real `docker stop` against a fresh
isolated PostgreSQL container. It never uses the production Compose project,
environment file, network or volumes, and removes its own containers on exit.

`scripts/backup.sh` — loops forever: once a day at `BACKUP_HOUR` (default 02:00 container local time), runs `pg_dump -Fc` to `/backups/audience_hub-YYYYmmdd-HHMM.dump`, verifies with `pg_restore --list`, deletes dumps older than `BACKUP_KEEP_DAYS`, logs one line per run.

The `BACKUP_DIR` should be on a different disk or an NFS/SMB mount that is itself backed up off-site.

## .env.example

```dotenv
APP_ENV=production
PUBLIC_BASE_URL=https://audience.obtv.io
POSTGRES_USER=audience
POSTGRES_PASSWORD=change-me
DATABASE_URL=postgresql+psycopg://audience:change-me@db:5432/audience_hub
SECRET_KEY=generate-with-openssl-rand-hex-32
FERNET_KEY=generate-with-python-cryptography-Fernet.generate_key
PII_HASH_PEPPER=generate-with-openssl-rand-hex-32
AUTH_MODE=oidc
OIDC_ISSUER=https://sso.obtv.io/application/o/audience-hub/
OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=
OIDC_ROLE_CLAIM=groups
OIDC_ADMIN_GROUPS=ah-admins
OIDC_ANALYST_GROUPS=ah-analysts
OIDC_VIEWER_GROUPS=ah-viewers
INGEST_CORS_ORIGINS=https://www.tbn.org,https://watch.tbn.org
DEFAULT_PHONE_REGION=US
EVENT_RETENTION_DAYS=730
EXPORT_RETENTION_DAYS=14
WORKER_CONCURRENCY=4
API_WORKERS=4
API_PORT=8080
BACKUP_DIR=/mnt/backups/audience-hub
BACKUP_KEEP_DAYS=14
DELETION_TWO_PERSON=true
LOG_LEVEL=INFO
```

## First install

```bash
git clone <repo> /opt/audience-hub && cd /opt/audience-hub
cp .env.example .env && nano .env        # fill secrets
docker compose build
docker compose up -d
docker compose logs -f api               # wait for "Application startup complete"
docker compose exec api python -m app.cli create-admin --email you@obtv.io   # optional bootstrap
```

Then in Authentik: create an OAuth2/OIDC provider (confidential, redirect URI `https://audience.obtv.io/auth/callback`, scopes `openid email profile`, include the `groups` claim) and an application, and create the three groups.

## Upgrades

### Server upgrade / dashboard rollups and checkpoint tuning

Run these commands on the server, from the directory containing this Compose
file and the production `.env`, for example
`~/audiencehub/artifacts/audience-hub` (not the repository root).
These are operator instructions, not commands to execute from development.
Arrange a maintenance window and block new imports/API writes at the reverse
proxy first. Do not restart PostgreSQL while imports or other jobs are running.
Never run `reset-demo`, seed loads, or benchmark tests against production.

```bash
cd ~/audiencehub/artifacts/audience-hub
set -e  # Stop this procedure if a backup, build, or migration fails.
# Read-only checks compatible with the OLD production image (c85dd6d).
# Repeat both queries while existing work drains; no new CLI is required.
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d audience_hub -c "SELECT id,status,rows_total,rows_ok,rows_rejected FROM imports ORDER BY id DESC LIMIT 20;"'
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d audience_hub -c "SELECT type,status,count(*) FROM jobs GROUP BY type,status ORDER BY type,status;"'
# Wait for running imports/jobs to finish; investigate failed or stuck work.
# Jobs finish as succeeded; imports finish as completed. Queued jobs persist
# across restart and can resume on the new worker. Do not start the DB restart
# while work is running. If work will not drain, defer the maintenance window.
# Stop writers gracefully; worker has a 30-minute stop grace period.
docker compose stop worker api
# Confirm graceful shutdown left no running work before backing up/restarting.
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d audience_hub -c "SELECT id,type,status FROM jobs WHERE status='\''running'\''; SELECT id,status FROM imports WHERE status='\''running'\'';"'
# Both result sets must be empty. Otherwise stop here and investigate.
mkdir -p backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d audience_hub -Fc' > "backups/pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).dump"
# Preserve an image for application rollback; keep .env and named volumes.
docker image tag audience-hub:latest "audience-hub:pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ)"
git pull --ff-only
docker compose config --quiet
docker compose build api
# Explicit maintenance-window database recreation activates command settings.
# This keeps pgdata. NEVER use docker compose down -v.
docker compose up -d --no-deps --force-recreate db
until docker compose exec -T db sh -c 'pg_isready -U "$POSTGRES_USER" -d audience_hub'; do sleep 2; done
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d audience_hub -c "SHOW checkpoint_timeout; SHOW max_wal_size; SHOW wal_compression; SHOW max_locks_per_transaction;"'
# Migrate with the new image before starting any new worker.
docker compose run --rm --no-deps api alembic upgrade head
docker compose up -d --no-deps api
docker compose exec -T api curl -fsS http://localhost:8000/readyz
```

Expected settings: `15min`, `16GB`, `on` (PostgreSQL may display `pglz`),
and the preserved `1024` lock budget. `max_wal_size` is a soft checkpoint target,
not a WAL disk-space cap; monitor free disk and backup/replication retention.
If readiness fails, investigate before starting the worker or reopening traffic.
The startup script also runs `alembic upgrade head`; the explicit migration step
makes failures visible before serving traffic.

Queue a full traits refresh through the **existing job queue**, so its normal
handler also refreshes dashboard rollups and invalidates shared dashboard cache.
There is no standalone `traits` CLI command:

```bash
docker compose exec -T api python - <<'PY'
from sqlalchemy.orm import Session
from app.db import engine
from app.jobs.queue import enqueue
with Session(engine) as db:
    job = enqueue(db, "traits.recompute", {"mode": "full"},
                  dedupe_key="upgrade:traits:full")
    db.commit()
    print("Full traits refresh job:", job.id)
PY
docker compose up -d --no-deps worker
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d audience_hub -c "SELECT id,type,status,error FROM jobs WHERE dedupe_key='\''upgrade:traits:full'\'' ORDER BY id DESC LIMIT 1;"'
docker compose exec -T api curl -fsS http://localhost:8000/readyz
```

Require the refresh job to be `succeeded`, not merely enqueued, before checking
Overview cards and issue badges with an authenticated admin session and reopening
traffic. `load-status` follows imports and identity work, not this manually
queued traits job: repeat the final jobs query until that job is `succeeded`.
The job queue uses `succeeded`; the separate imports table uses `completed`.
A failed refresh needs investigation; do not substitute a request-time
raw-data rebuild. The local performance command
`python -m app.cli benchmark --dashboards --profiles 500000` must run only in
its disposable harness on a suitably sized test host, not against production.
Keep the backup until the upgrade and rollups are verified. Application rollback
requires a retained image compatible with the migrated schema; do not blindly
downgrade the database or restore over live data.
Migrations must be backward compatible for one release (add columns nullable, backfill in a job, then tighten), so a failed deploy can roll back to the previous image tag.

Tag images per release (`audience-hub:2026.10.1`) as well as `latest`, and keep the last three.

## Existing synthetic audit seed: opt-in repair (not reset/reseed)

Run **only on your own server**, against the intended existing database. Retain
a verified database backup/snapshot first; a journal is not a backup. Do not
regenerate CSVs, reload imports, reset profiles, or rescale positive gifts. The
reported production median of 49.77 is already in currency units and is not an
error. Counts such as 52 unlinked refunds and 173,903 donation-source profiles
without gifts are observations, not hard-coded repair targets.

```bash
# Default is PostgreSQL-enforced read-only, including connection startup.
docker compose exec -T api python -m app.cli repair-seed
# After reviewing counts/backup and letting pending work finish:
# Stop API traffic and the worker; do not cancel or discard unfinished jobs.
docker compose stop api worker
docker compose run --rm --no-deps api python -m app.cli repair-seed --apply --yes
# Restore service after success (or after a reported rollback):
docker compose start api worker
# Independent read-only reconciliation after the committed repair:
docker compose exec -T api python -m app.cli verify-seed
```

Both write flags are required; apply refuses a Replit-marked environment and
reports which marker names caused the refusal (never their values). APP_ENV
is not a server-location check: a self-hosted container using the default
development setting receives a notice, without changing its auth configuration.
The command never contacts Replit or another
server: it uses this server's configured `DATABASE_URL`. Schedule a maintenance
window with no imports, ingestion or queued/running jobs. Apply refuses busy
jobs/imports, pending resolution and conflicting table locks; it takes a
transaction advisory lock plus write-excluding table locks to fence even
non-cooperating writers. It does not cancel work or change worker configuration.
Dry-run takes no write locks and makes no database/audit/temp-table writes; its
snapshot is informational and all preconditions are repeated under apply locks.

Scope requires deterministic seed IDs, original seeded-file/contact provenance,
reserved-domain email (or hash-only contact evidence), same-person explicit
identity references and JSON boolean `context.synthetic=true` on corresponding
events. Mixed, ambiguous, incomplete, deleted/merged, or unproven data fails
closed; this is intentionally not a general-purpose production-data repair.
Destination namespace collisions abort the entire transaction. Existing
nonfinancial events and their source records move from `donation_events` to
`people_feed` without changing row IDs, profile IDs, messages or external IDs.
Existing resolver identifiers, master IDs, canonical identity keys and consent
are untouched; identity resolution is deliberately not rerun.

Each deterministic `donation_return-i-recent-j` must have exactly one same-person,
same-source original `gift:donation-i-recent-j` (or the corresponding explicit
legacy ID), matching currency and non-reversed known chronology. Only that
refund's amount is corrected to the exact negative original amount, once;
duplicate/other returns against the original abort. Missing dates are allowed,
not fabricated. Undated ledger entries count toward lifetime giving; future
entries do not yet count. A future refund does not remove today's original
from the unreturned-gift count. Latest donation uses the latest unreturned dated
gift, with deterministic ID tie-breaking; an undated-only gift has a null date.
Canonical lifetime amount is net ledger total; count, average and latest-gift
fields reflect unreturned gifts. Positive original gift amounts never change.

The existing traits pipeline refreshes the scoped synthetic profiles, source
chips and shared dashboard rollups/cache **inside the repair transaction**.
No queued work is needed. Its established trait count/average semantics remain
unchanged (positive ledger gifts, including subsequently returned gifts);
canonical contact count/average intentionally describe unreturned gifts.
Historical monthly snapshots and materialized segment memberships are not
rewritten. Use the normal segment refresh UI/job pipeline if needed afterwards.

Every changed base row has a full `audit_log` before image under
`action='seed.repair.before'`; `details.run_id` groups it with the
`seed.repair.complete` summary. Event before images include their composite
`id,occurred_at` key. Restrict access/retention as for other contact audit data.
Recovery is a reviewed DBA operation from these before images or the retained
backup, in another exclusive maintenance window; there is deliberately no blind
undo command that could overwrite later legitimate changes. Any failure rolls
back base changes, journal, traits and rollups together. A reconciled repeat
invocation makes no further changes or journal entries.

## Restore

The following is a separate disaster-recovery procedure, **not a seed repair
step**. It replaces database contents with the retained backup.

```bash
docker compose stop api worker
docker compose exec -T db pg_restore -U audience -d audience_hub --clean --if-exists < /mnt/backups/audience-hub/audience_hub-YYYYmmdd-HHMM.dump
docker compose start api worker
```
Test a restore into a scratch database monthly.

## Logging

Containers log JSON to stdout. To ship to Graylog, add to each service:
```yaml
    logging:
      driver: gelf
      options:
        gelf-address: "udp://graylog.internal:12201"
        tag: "audience-hub-{{.Name}}"
```

## Moving code from Replit to the server

1. Connect the Replit project to a private Git repo (GitHub or the self-hosted Git server) and push from Replit.
2. Clone on the server and deploy as above. Never copy Replit's `.env`/Secrets to the server; production secrets are created fresh on the server.
3. Remove or ignore Replit-only files in the image with `.dockerignore`: `.replit`, `replit.nix`, `.config/`, `.cache/`, `node_modules/`, `__pycache__/`, `.pythonlibs/`, `.upm/`, `*.env`, `backups/`.
