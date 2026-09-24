# Milestones

Build in this order. Each milestone ends with its acceptance checks passing, tests green (`pytest -q` and `npm run build && npm run lint`), the README updated, and a short summary to me.

## M1: Foundation

- Repo layout from the prompt, `pyproject.toml` with pinned deps, frontend scaffold (Vite + React + TS + Tailwind + Router + Query), theme tokens from `UI.md`, bundled fonts.
- `config.py` (pydantic-settings), `db.py`, Alembic initialized; first migration with `users`, `audit_log`, `jobs`, `scheduled_runs`, `sources`.
- Auth: dev mode and OIDC mode, role guards, CSRF, `/api/me`, the startup guard for dev auth in production.
- Job queue (`jobs/queue.py`): enqueue with dedupe key, claim with `SKIP LOCKED`, heartbeat, retry with backoff, stale-job requeue. Worker process and scheduler loop with `scheduled_runs` dedupe.
- App shell: sidebar, top bar, job activity drawer, empty pages for each section, System page showing the job queue.
- `/healthz`, `/readyz`, JSON logging with PII redaction filter, security headers.
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `scripts/*.sh` per `DEPLOYMENT.md`. `scripts/dev.sh` and `.replit` for Replit.
- `app.cli` with `create-admin` and `seed` commands (seed is a stub until M2).

Acceptance: log in with dev auth as each role and see the right sidebar items; enqueue a `noop` job from the System page and watch it complete; the worker survives being killed mid-job (the job is requeued). Tests: queue claim/heartbeat/requeue, role guards, dev-auth startup guard.

## M2: Data model, synthetic data, CSV import

- Migrations for all remaining tables in `DATA_MODEL.md` including monthly `events` partitions and the `active_profiles` view.
- Normalization module with the full test list from `IDENTITY_RESOLUTION.md`.
- Seed generator (`python -m app.cli seed --profiles 50000 --scale small|medium|large`), deterministic with `--random-seed`:
  - Writes CSV files (not direct DB inserts) so they exercise the import path: `donor_crm_contacts.csv`, `giving_platform_gifts.csv` (5 years of gifts with realistic seasonality peaking in November/December, recurring monthly donors ~15% of donors, amounts log-normal around $50, funds like General, Missions, Building, Media; campaigns and appeal codes), `esp_contacts.csv` (with consent columns and some hard bounces), `five9_calls.csv` (as events), `zeta_enrichment.csv` (fake vendor attributes: `hh_income_band`, `age_band`, `interests` enum, `donor_propensity_score` number).
  - Deliberate overlap and messiness: ~40% of CRM contacts also in the ESP file with case/whitespace/gmail-dot variations, ~10% phone-only matches, a few junk emails from the blocklist, duplicate rows, invalid phones, mixed date formats.
  - Uses the Faker library with `en_US` locale. Every generated email uses a reserved domain that can't reach a real mailbox (`example.com`, `example.org`, `example.net`). To exercise the gmail normalization rules, the generator also emits dot and `+tag` variants at `gmail.test`, and the normalizer reads its gmail-style domain list from `GMAIL_STYLE_DOMAINS` (default `gmail.com,googlemail.com`; set to `gmail.com,googlemail.com,gmail.test` in dev). Phone numbers are random Faker values that pass `phonenumbers.is_valid_number`; they are never dialed or sent anywhere outside the dev database.
  - `--load` flag runs the imports through the real import job after generating files.
- Sources page (CRUD, priority ordering).
- Import wizard end to end: upload (streamed), preview, mapping with auto-suggest, validation dry run, run with progress, error CSV. `COPY`-based staging, batched upserts, suppression check, idempotent re-import.

Acceptance: `seed --profiles 50000 --load` completes; the import list shows accurate ok/rejected counts; re-running the same import changes nothing. Tests: mapping auto-suggest, validation errors per record type, idempotency.

## M3: Identity resolution and profiles

- Resolver, merge, survivorship, blocklist, high-cardinality guard per `IDENTITY_RESOLUTION.md`, with every required test.
- `identity.resolve_batch` enqueued after imports and event ingestion.
- Profiles API: `GET /api/profiles?search=&source_id=&has_email=&has_phone=&donor_status=&page=&page_size=` returns `{items,total,page,page_size}`; search uses PostgreSQL trigram matching.
- Profile detail: `GET /api/profiles/{id}` returns scalar fields, traits and the gifts, events, identifiers, source_records, merges, enrichment, and consents arrays; viewer contact data is masked recursively and each view is audited.
- Data Health API: `GET /api/data-health` reports unresolved source records, daily merges, source-record blocklist hits, high-cardinality auto-blocklists awaiting review, rejected rows, and recent imports. Admins can approve (retain block) or unblock (remove block) using the paired blocklist actions.

Acceptance: after seeding, the number of profiles is within 2% of the generator's known ground-truth person count (the generator writes `ground_truth.json` mapping every record to its true person ID); precision check reports the share of profiles containing records from more than one true person, which must be under 1% (excluding intentional shared-household cases the generator marks). Tests: all listed in `IDENTITY_RESOLUTION.md`.

## M4: Computed traits and dashboards

- Trait registry and set-based recompute, dirty-profile incremental mode, nightly full recompute, `trait_snapshots`.
- Dashboard endpoints and pages: Overview, Giving, Retention, Engagement, Sources (Data Health already done).
- Chart "table" toggle and CSV download.

Acceptance: trait values match a slow Python reference implementation on 1,000 random seeded profiles; nightly recompute on the medium seed (500k profiles) finishes under 10 minutes on the dev machine or is documented with timing. Tests: donor status boundaries, RFM quintiles, pinned `as_of`.

## M5: Segments

- DSL schema, field registry, compiler, preview/sample endpoints with timeout, materialization, count history, segment-in-segment with cycle checks.
- Builder UI per `SEGMENTS.md` and `UI.md`, including summary sentence and JSON view.
- Seed the six template segments.

Acceptance: all six templates return plausible counts on seeded data; preview under 5 s on the medium seed. Tests: all listed in `SEGMENTS.md`.

## M6: Event ingestion and SDK

- `/v1/track|identify|page|batch` with write keys, validation, idempotency, rate limiting, CORS, IP truncation.
- `identify` → source record → resolution → anonymous event back-fill.
- `/sdk/ah.js` per `API.md` (write it in plain TypeScript compiled by Vite into a separate bundle), SDK snippet on the event source page, live event tail.
- Seed generator gains `--events` to simulate web and app traffic via the real API.

Acceptance: a static test page served in dev loads the SDK, tracks page views anonymously, then identifies, and the profile timeline shows the earlier anonymous events. Tests: auth, idempotency, batch limits, clamping, back-fill.

## M7: Activation, consent, deletion

- Destinations (CSV, Google, Meta, webhook) with Fernet-encrypted secrets and SSRF guard.
- Activation wizard, runs with funnel counts, schedules, delta mode, run history, downloads with audit.
- Consent precedence, manual consent change, suppression list, deletion request flow with two-person approval.

Acceptance: a full run for each destination type on a seeded segment; webhook delivers to a local echo server with a valid signature; a deletion request removes the profile everywhere and a re-import of the seed files does not recreate it. Tests: all listed in `ACTIVATION.md`, consent precedence, deletion completeness (query every table for the profile ID afterwards).

## M8: Hardening and handoff

- Admin pages complete (users, blocklist, deletion requests, audit log, settings).
- Load test script (`scripts/loadtest.py`) for `/v1/batch` and segment preview, results in the README.
- `enrichment.expire`, `maintenance.retention`, upload/export retention, partition creation ahead.
- `pip-audit` and `npm audit` clean.
- README: architecture diagram, setup for Replit dev, production deploy summary pointing to `DEPLOYMENT.md`, operations runbook (restart, restore, rotate write key, rotate `FERNET_KEY` procedure, add a new CSV source, onboard an enrichment vendor file).
- `docs/DECISIONS.md` complete.

Acceptance: `docker compose up -d --build` on a clean Linux VM, followed by `docker compose exec api python -m app.cli seed --profiles 50000 --load`, gives a working system reachable through the reverse proxy with OIDC login.
