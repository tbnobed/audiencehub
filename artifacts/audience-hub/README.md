# Audience Hub

Self-hosted customer data operations console for OBTV/TBN. Milestones 1 and 2 provide login, a role-aware console, PostgreSQL job processing, the full data schema, synthetic CSV generation, source management, and CSV imports. No real donor data is included.

## Development

Requires Python 3.12, Node 20+, npm, PostgreSQL 16, and the variables in `.env.example`. Use a disposable PostgreSQL database with **synthetic data only**. For Replit the preconfigured `DATABASE_URL` is used; do not import real records.

From this directory: `python3 -m pip install ./backend[test]`, `npm ci`, then `bash scripts/dev.sh`. The launcher migrates the database and starts the API on 127.0.0.1:8000, worker, and Vite on port 5000 (or `PORT` if provided by the development preview). Vite proxies `/api`, `/v1`, `/sdk`, and `/auth` to the API. In dev mode choose a role on the sign-in screen; production rejects `AUTH_MODE=dev`.

Check `GET /healthz` and `GET /readyz`. Run backend checks from `backend/` with `pytest -q`; frontend checks here with `npm run build && npm run lint`. Development uploads and exports go into the ignored `.data/` directory; production uses the `/data` volume.

## Synthetic imports (M2)

Run `cd backend && python -m app.cli seed --profiles 50000 --scale small --load` with the same database, secret and `UPLOAD_DIR` environment as the development server. This produces five CSVs and `ground_truth.json` under `backend/seed-data/`, then enqueues all five files through the same `import.run` worker path as the UI and waits for accurate per-import counts. `--random-seed` makes generation reproducible; `--scale small|medium|large` changes the event and one-time-gift density. Use `--output-dir` to retain the files elsewhere. A repeat with identical files is skipped after a completed import. The ground-truth file maps every generated record to its true person and marks shared households; it is intentionally ignored by Git.

In production, use `docker compose exec -w /app/backend api python -m app.cli seed --profiles 50000 --scale small --output-dir /data/seed-data --load` so the generated files and ground truth persist on the shared volume. The worker service must be running. The `/sources` page manages source definitions and priority, while `/imports` uploads CSVs and guides mapping, dry-run validation, queued processing, progress, and error CSV downloads. Source deletion is refused if historical imports or records reference it; deactivate instead.

M2 imports retain unlinked source records and gifts/events until the M3 identity resolver is implemented. Profile matching, the M3 precision report, activation, and ingestion are not included here.

## Worker recovery (M1)

`JOB_STALE_SECONDS` defaults to 300. For an isolated, migrated PostgreSQL test database, set it to 2 and run `python scripts/manual_worker_recovery.py --stale-seconds 2 --sleep-seconds 60 --timeout-seconds 90` from this directory. The script starts its own worker, kills it during a synthetic 60-second sleep, starts a new worker, and prints the terminal status and attempt count. Do not run this against a database with another active worker that could claim the synthetic job. The opt-in pytest subprocess case uses `AH_WORKER_RECOVERY_TEST_DATABASE_URL`.

## Production

Copy `.env.example` to `.env` on your Linux server and set new, strong production-only secret values, OIDC Authentik settings, database password, and public URL. Then run `docker compose up -d --build`. The Compose stack contains only Postgres 16, FastAPI, a separate worker, and a daily pg_dump backup container. It binds the API on localhost for an existing TLS reverse proxy. No Replit service, CDN, telemetry endpoint, or hosted asset is used by the production image.

The API serves the built React files. Routes are `/api/*` for the UI, `/auth/*` for login, `/healthz` and `/readyz`. `/v1/*` and `/sdk/*` are reserved for the ingestion milestone. Operational data lives in PostgreSQL; jobs are claimed with `FOR UPDATE SKIP LOCKED`, heartbeated, retried, and requeued after stale heartbeats.

See `docs/MILESTONES.md` for subsequent milestones and `docs/DECISIONS.md` for implementation choices.