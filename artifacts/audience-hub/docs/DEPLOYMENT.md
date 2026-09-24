# Deployment (self-hosted Docker)

Production runs on our own Linux server with Docker Engine and the Compose plugin. Replit is only used to develop the code. Nothing in production depends on Replit.

## Host requirements

- Ubuntu 22.04/24.04 or Debian 12, Docker Engine 24+, Compose v2.
- 8 vCPU, 32 GB RAM, 500 GB SSD for the MVP scale targets (Postgres data on SSD).
- Sits behind the existing reverse proxy that terminates TLS (Nginx Proxy Manager, Traefik, or Caddy). Only the proxy is exposed; `api` binds to the internal Docker network or `127.0.0.1`.
- DNS: e.g. `audience.obtv.io` for the UI/API. If the browser SDK is used on public sites, the `/v1` and `/sdk` paths must be reachable publicly; everything else can be restricted to the internal network or VPN at the proxy.

## Dockerfile (multi-stage, single image)

```dockerfile
# ---- frontend build ----
FROM node:20-bookworm-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # outputs /web/dist

# ---- python runtime ----
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 tini curl \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY backend/pyproject.toml ./
RUN pip install --upgrade pip && pip install .
COPY backend/ ./
COPY --from=web /web/dist ./static
COPY scripts/ ./scripts/
RUN chmod +x scripts/*.sh && mkdir -p /data/uploads /data/exports && chown -R app:app /data /app
USER app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./scripts/start-api.sh"]
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
               -c max_wal_size=8GB -c random_page_cost=1.1
               -c jit=off
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
    command: ["./scripts/start-api.sh"]
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
    command: ["./scripts/start-worker.sh"]
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

```bash
cd /opt/audience-hub && git pull
docker compose build
docker compose up -d        # api runs alembic upgrade head on start
```
Migrations must be backward compatible for one release (add columns nullable, backfill in a job, then tighten), so a failed deploy can roll back to the previous image tag.

Tag images per release (`audience-hub:2026.10.1`) as well as `latest`, and keep the last three.

## Restore

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
