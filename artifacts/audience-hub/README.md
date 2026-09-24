# Audience Hub

Self-hosted customer data operations console for OBTV/TBN. Milestone 1 is the foundation only: login, role-aware navigation, an admin job queue and worker, an initial PostgreSQL schema, and portable packaging. No donor data is included.

## Development

Requires Python 3.12, Node 20+, npm, PostgreSQL 16, and the variables in `.env.example`. Use a disposable PostgreSQL database with **synthetic data only**. For Replit the preconfigured `DATABASE_URL` is used; do not import real records.

From this directory: `python3 -m pip install ./backend[test]`, `npm ci`, then `bash scripts/dev.sh`. The launcher migrates the database and starts the API on 127.0.0.1:8000, worker, and Vite on port 5000 (or `PORT` if provided by the development preview). Vite proxies `/api`, `/v1`, `/sdk`, and `/auth` to the API. In dev mode choose a role on the sign-in screen; production rejects `AUTH_MODE=dev`.

Check `GET /healthz` and `GET /readyz`. Run backend checks from `backend/` with `pytest -q`; frontend checks here with `npm run build && npm run lint`.

## Production

Copy `.env.example` to `.env` on your Linux server and set new, strong production-only secret values, OIDC Authentik settings, database password, and public URL. Then run `docker compose up -d --build`. The Compose stack contains only Postgres 16, FastAPI, a separate worker, and a daily pg_dump backup container. It binds the API on localhost for an existing TLS reverse proxy. No Replit service, CDN, telemetry endpoint, or hosted asset is used by the production image.

The API serves the built React files. Routes are `/api/*` for the UI, `/auth/*` for login, `/healthz` and `/readyz`. `/v1/*` and `/sdk/*` are reserved for the ingestion milestone. Operational data lives in PostgreSQL; jobs are claimed with `FOR UPDATE SKIP LOCKED`, heartbeated, retried, and requeued after stale heartbeats.

This milestone does **not** provide CSV import, profiles, dashboards, segment computation, activation, or ingestion. `python -m app.cli seed` fails clearly until Milestone 2. See `docs/MILESTONES.md` for future milestones and `docs/DECISIONS.md` for implementation choices.