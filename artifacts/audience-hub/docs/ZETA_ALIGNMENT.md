# Zeta replacement acceptance

The authoritative requirements are the October 17, 2026 account audit supplied
by the user, plus the audience catalog in `ZETA_AUDIENCE_TARGETS.json`.
Implementation proceeds in the audit's eight-step order. Email/SMS sending,
journeys, overlays, AI and Zeta insight products are excluded. Google/Meta
destinations remain unchanged; further M6 SDK work waits.

## Audience acceptance

For a fixture of N profiles, expected audience count is
`N * real_count / 7,807,899`. Absolute allowed error is the larger of
`0.20 * expected_count` and 25 records. No missing counts are inferred.
Giving Tuesday 2024 gifts use **December 3, 2024**, not a shifted holiday.
The catalog supersedes the earlier approximate proportions when they differ,
including premium upgraders.

## Step 1: baseline findings before changes

Existing profile IDs are database-generated numeric identities. Both resolver
paths choose the earliest `first_seen_at`, then lowest numeric ID. Merges move
references to the survivor, retain losing profiles with `merged_into_id`, and
record a merge row. Profile lookup reports an immediate merge target.

There was no permanent Master ID, alias-chain lookup or unmerge operation.
Existing merge rows did not capture enough per-side provenance to safely
reverse gifts, events, identifiers, consent/enrichment consolidation and derived
traits. Historical merges without provenance must not be advertised as
reversible.

These baseline findings are code inspection, not performance evidence.
The new implementation must retain immutable public IDs, reject ambiguous
contact-key merges, and capture provenance before any merge mutation.
Unmerge must not resurrect deleted data or overwrite later changes silently.

## Verification reporting

Each implementation step must report actual tests and measured timings.
The 10k-key lookup target is below two seconds at 500k profiles; a small-fixture
test is not proof of that target. Likewise, projections for 8M profiles must
identify the measured basis and uncertainty. No 8M load is authorized.

## Step 1 implementation and measured evidence

Implemented immutable `kin_` ULIDs, the permanent issuance registry, resumable
backfill, alias-chain lookup, indexed crosswalks, system-key conflict reviews,
hash-only matching and duplicate-HubSpot reporting. Both resolver paths journal
per-side source records, identifiers, gifts, events, traits, consent and
enrichment before merging. The admin unmerge endpoint restores attributable
sides, retains the original public identity with the oldest-system-key side,
and issues a never-before-used ID to the other side. Later ambiguous mutations
produce a conflict rather than being overwritten.

Profile detail/preview show a copyable Master ID; Profiles search accepts
current and retired IDs. Data Health shows coverage, conflicts, hash-only
profiles and duplicate HubSpot keys. General CSV/webhook exports always include
Master ID; Google/Meta payload formats are unchanged.

SFTP lookup supports donor account, HubSpot and Okta CSV columns, encrypted
private-key configuration, required host-key pinning, DNS-pinned public targets,
explicit unknown/ambiguous results and atomic idempotent output publication.
SFTP transport is mock-tested, not connected to a production server.

### Measured tests

- Disposable 500,000-profile / 500,000-crosswalk-row fixture: 64.303 seconds
  setup. Real FastAPI bulk lookup of 10,000 keys, including audit commit and
  JSON response: **0.869 seconds**. This is a local measurement, not a
  production SLA; a prior run measured 1.544 seconds.
- Initial focused core checks: 52 passed in 99.34 seconds; real application
  routing/session/CSRF check passed in 5.54 seconds.
- Browser fixture: admin unmerge succeeded; gift, event and identifier
  redistribution confirmed in the database and separated profile UI. Retired
  alias and hash lookup resolved correctly. Analyst unmerge was disabled.
- Browser found a viewer leak in the legacy profile identifiers table. The
  server now redacts identity-key values and free-form merge reasons for
  viewers; the real-PostgreSQL detail-response regression passed afterwards.
  Public, non-PII Master IDs remain visible.
- Initial full suite: **13 failed, 623 passed, 2 skipped** in 1197.57 seconds.
  Do not describe that run as green. Original evidence is retained in
  `backend/.cache/master-integrated-suite/imports-8vovj4on/pytest.txt`.
- Follow-up core regression set: **31 passed** in 108.37 seconds, including
  source-write minimization, bounded resolver query count, privacy masking,
  reset inventory and migration-head checks.
- Final extended 50k import/identity/traits/segment acceptance and activation
  export-order regression: **2 passed** in 373.16 seconds. Evidence:
  `backend/.cache/master-segments-acceptance/imports-vjx1pq9f/pytest.txt`.
  All initially failing cases were covered by passing targeted reruns;
  the full suite has not been rerun as a single final pass.

### Deployment sequence (not performed in production)

From `artifacts/audience-hub/backend`, with production writers stopped:

```sh
alembic upgrade head
kinship master-id backfill --batch-size 5000
```

Complete the backfill before resuming resolver jobs. Backfill commits each
batch and can resume after interruption. The registry survives demo reset so
issued IDs are never reused. Pre-journal historical merges cannot be safely
unmerged and are explicitly unavailable.

## Step 2: canonical model and sensitive data

The canonical field catalog contains 81 typed first-party fields, including
imported model-date links. Imported values use source-priority evidence; they
never silently fall back to computed Kinship traits. Legacy aliases normalize
to snake_case, with conflicting duplicate values rejected rather than lost.
The profile UI displays grouped imported evidence separately from computed RFM.

`source_codes`, `products` and `vod_titles` are first-class tables with COPY
upserts, resource APIs and validated gift/event references. Segment conditions
can filter resource keys, names and VOD series. All twelve audit event names are
supported. Vendor enrichment carries vendor, license, append date and
sensitivity metadata. Unknown historical metadata is not fabricated.

Viewer responses omit sensitive attributes. Religion, politics and military/
veteran fields are always sensitive. Export mappings distinguish canonical
first-party attributes from computed traits. Sensitive exports require an exact,
explicit admin-approved field set; queued execution and downloads revalidate
approval. Delivery and download audits record field names, not sensitive values.

Verification:
- Canonical/unit/import mapping tests: 57 passed in 1.31 seconds.
- Initial combined isolated PostgreSQL run: 69 passed, 15 failed; four canonical
  acceptance tests passed. Failures were fourteen malformed test-fixture SQL
  literals and one outdated synthetic enrichment fixture missing license data.
- Corrected export/privacy plus activation suite: 41 passed in 86.93 seconds.
- Corrected enrichment import/synthetic evidence tests: 3 passed in 1.61 seconds.
- Profile frontend: 132 passed; segment model tests: 9 passed; TypeScript passed.
- Synthetic seed enrichment now has explicit synthetic-only vendor/license
  evidence. No license metadata was invented for real historical records.

## Step 3: scheduled source ingestion

Sources now support SFTP JSONL, read-only Snowflake key-pair pulls and Zeta exit
imports. Credentials are Fernet encrypted and write-only in the admin UI.
SFTP requires pinned host keys and public-address validation; Snowflake accepts
restricted SELECT queries or table mappings, with inclusive incremental cursors.
Dry runs inspect up to 100 rows per mapping without returning record values.

Durable content receipts and immutable spools feed checkpointed COPY imports.
All contact-bearing files in a pull finish before its deferred identity pass.
Donation events also produce idempotent gifts; returns produce refund-tagged
negative ledger entries without inflating donor counts or gift recency.
Licensed vendor enrichment remains separate from first-party properties.

The Sources connection dialog supports mappings, schedules, saved credential
replacement, dry runs, manual pulls and persistent polled job history. Recovery
blocks configuration edits but permits resuming the pending pull. Reset clears
pull receipts/cursors while preserving encrypted source configuration, and
checks table/advisory locks before mutating anything.

Verification:
- Source/import/traits/rollup PostgreSQL checks: 56 passed in 48.09 seconds.
- Source connector unit checks: 30 passed in 0.81 seconds.
- Connection/history UI checks: 5 passed against mocked API; TypeScript passed.
- No live SFTP or Snowflake connection has been attempted.

## Step 4: audit seed

The default seed now follows the supplied population targets; the former generic
mix is available only through explicit legacy mode. UTC-relative dates and the
fixed December 3, 2024 Giving Tuesday exception are preserved. Generation is
streamed rather than retaining the population in memory.

Fresh 50k import acceptance passed in 231.26 seconds: exactly 50,000 profiles,
zero unresolved records/facts, all audience proportions passing, and a nonempty
dashboard. Full independent SQL counts and per-phase timings are in
`AUDIT_SEED_VERIFICATION.md`.

## Step 5: audience templates

All 23 audited templates are implemented using canonical evidence and event/
resource predicates, not synthetic cohort tags. At 50k every compiled membership
set matched the independent SQL oracle exactly; counts, previews, samples and
materializations passed. Separate adversarial PostgreSQL tests passed (5 tests,
7.80 seconds). The 500k performance result is recorded separately in
`AUDIENCE_ACCEPTANCE.md` when available; 50k results are not a 500k claim.

## Step 6: scheduled SFTP delivery

SFTP destinations support pinned host keys, encrypted key authentication, cron
with IANA timezones, selected/all non-sensitive attributes, optional gzip and
manifest-last publication. Manifests contain row counts, transmitted-byte sizes
and SHA-256 checksums. Failed deliveries appear in run history and Data Health.

CustomerPulse (monthly days 2 and 1), Trilogy (day 29) and a daily crosswalk
configuration are available as disabled reference presets. Cron day 29 skips
nonexistent dates rather than silently using February 28.

Crosswalk bundles include both key mappings and aliases, with admin-only
authorization, an explicit purpose, consent and suppression filtering. Local
privacy deletion purges CSV, gzip and alias siblings. Remote recipients remain
responsible for downstream erasure; no Snowflake or remote deletion writer has
been implemented.

Verification:
- SFTP/deletion focused PostgreSQL run: 7 passed in 11.44 seconds.
- Additional real crosswalk/permissions/privacy-erasure test: 1 passed in
  14.30 seconds, including manifest checksums and filtered aliases.
- Activation frontend logic: 20 passed; profile rendering: 132 passed;
  TypeScript passed.
- Remote delivery tests use a fake SFTP server interface, not production.

## Step 7: operational fixes

`run_import` applies the configured bulk statement timeout on every checkpoint
transaction, including direct in-process callers, without leaking it back into
pooled connections. Timeout/recovery checks passed: 18 tests in 19.02 seconds.

Plain `kinship benchmark --segments` now uses a dedicated read-only connection
against existing data and measures all 23 templates without installing,
seeding, materializing or auditing anything. Materialization is explicitly
skipped. `--isolated` retains the disposable synthetic mode. PostgreSQL tests
verify INSERT/UPDATE/DELETE/temp-table refusal, unchanged durable rows and
cleanup on failure/SIGTERM. The focused safety/CLI suite passed 13 tests in
12.81 seconds.

## Step 8: scale plan

`SCALE_8M.md` contains workload-aware estimates, partitioning and identity
parallelism constraints, index strategy, disk/RAM allowances and release gates.
CLI parse tests accept 8,000,000 profiles with large scale without invoking
generation. Compact ground truth is expanded through bounded disk-backed
processing, not a population-sized dictionary; the real PostgreSQL accuracy
fixture passed in 8.58 seconds. **No 8M load was run.**

## Final verification status

The broad backend regression run reported **762 passed, 10 failed, 2 skipped,
4 deselected** in 1465.93 seconds. All ten failures were subsequently addressed
in targeted checks (identity orchestration/statistics, webhook mapping fixtures,
and production-configured benchmark dispatch); the entire suite was not rerun
as one final pass. The trait discrepancy was investigated separately: refund
rows incorrectly influenced some RFM/snapshot recency and frequency calculations,
and the independent reference missed canonical VOD events. Those were corrected;
21 focused tests, including an actual audit-seeded CLI trait comparison, passed
in 35.66 seconds with zero discrepancies.

The audit harness now supplies the worker's 7200-second transaction budget for
identity/traits/rollups instead of inheriting the API's 60 seconds, and preserves
phase timing evidence on failure. Eight focused PostgreSQL checks passed in
6.71 seconds, including cleanup and preservation of the API preview limit.
The 500k population has **not** been rerun after this harness correction.

Frontend typecheck and production build passed. Profile render tests passed
132 cases; activation logic passed 20. The preview starts and its sign-in page
was visually checked. The earlier signed-in browser pass verified Master ID
search and admin unmerge; later source/export UI checks use rendered/unit
fixtures rather than a second signed-in browser pass.

The **500k audience acceptance remains not passed**; see
`AUDIENCE_ACCEPTANCE.md`. Production data and remote SFTP/Snowflake services
were not changed. Do not interpret the implemented feature list or the 50k
acceptance as completion of that remaining scale gate.
