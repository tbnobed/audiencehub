# Worker lifecycle, M7 follow-ups, UI completion and M6 verification

## Scope

No production commands or data changes were performed. The existing CSV import,
identity-resolution and transactional attachment modules remain unchanged while
the user measures the 500k production load. M6 calls the existing resolver through
a separate ingestion job and adds source-scoped anonymous-event backfill.

## Worker lifecycle

SIGTERM stops claims, cancels owned statements, terminates remaining attempt-owned
sessions, then queues retry. Ownership validation occurs before handler dispatch,
including non-SQL side effects. Stale recovery fences connection registration and
terminates only sessions owned by that attempt in the same database and role.
API/worker sessions have identifiable names, bounded statement/idle-transaction
timeouts and keepalives. Compose grace is 30 seconds.

Real PostgreSQL subprocess tests cover supervisor/child SIGTERM, concurrent helper
queries, advisory-lock waits, disappearance of owned backends, retry, unrelated
API-session survival, stale recovery and obsolete-attempt rejection.

**Exact Docker acceptance is blocked, not passed.** The release image built, but
the isolated Docker PostgreSQL container's healthcheck could not execute:
`OCI runtime exec failed ... setns ... exit status 1`. The test never reached
`docker stop`. The disposable Docker resources were cleaned up. The reproducible
test is `scripts/worker_shutdown_acceptance.py`; no production run is claimed.

## M7 and UI

- Multi-value erasure preserves unaffected list values; scalar/emptied predicates
  fail closed in their boolean context. Segments, dependencies and activations
  remain enabled; affected materializations are refreshed.
- Changes are audited without erased values and create durable Data Health review
  issues. Negative audiences can broaden when exclusions shrink: review warnings
  explicitly disclose this continuity tradeoff.
- Browser-verified analyst activation access; destination creation and deletion
  approval returned 403. Viewer activation/admin routes redirected, restricted
  APIs and downloads returned 403. Contact fields were masked in list, preview and
  full profile. Viewer consent controls were read-only.
- A successful analyst CSV download was not exercised: existing files were expired
  and the seeded activation was paused. No existing activation was modified.
- Route-split build: initial JS chunk approximately 333.21 kB; no emitted JS chunk
  exceeds 500 kB. Frontend build and TypeScript checks pass.
- Profile regression tests: 125 passed. Timeline tests: 2 passed.
- Historical commit IDs and completed preview/timeline gaps are recorded in
  `UI_MERGE_VERIFICATION.md`.

## M6

Implemented write-key ingestion, payload/depth/batch limits, idempotency receipts,
timestamp clamping, token-bucket rate limits, explicit-origin CORS and IP
truncation. Keys are hashed at rest; beacon authentication is in the body, never
in a URL. SDK snippet URLs contain a build-generated SHA-256 content hash.

The plain-TypeScript SDK is 3,745 bytes minified. Its 20-test harness passes,
including queueing, timed/size flush, stable retry IDs, cookies/storage fallback,
opt-out, GPC, pagehide, bounded queue and packaging checks. Docker packaging copies
both SDK build inputs and outputs; development startup builds SDK assets.

Browser acceptance created only a new test event source/profile:

1. Generate a key in Sources; load the static SDK test page.
2. Send an anonymous page view and track event, then identify.
3. Receive HTTP 202; source tail associates all three events with one profile.
4. Open the profile Activity timeline and reload: all three events remain.

The first profile navigation exceeded the tester's short timeout during the
concurrent backend suite. A focused recheck with a longer wait rendered correctly,
with no failed module requests or console errors. Browser opt-out/GPC was not
repeated; those behaviors are covered by the SDK harness.

Migration head is `0028_event_ingestion`. Deletion tests include pending identifies,
anonymous history, resolved history, suppression on re-ingestion and every-table
profile-ID checks. `seed --events` uses real HTTP ingestion.

## Backend suite

An initial run was interrupted by workspace restart and has no valid total.
The next complete run exposed nine failures (531 passed, 1 skipped); fixes included
stale test fixtures and a genuine obsolete-worker-attempt dispatch guard.
The focused correction run passed 56 tests, including all nine failures and four
new ownership regressions.

**Final complete suite: 544 passed, 1 skipped, 0 failed**, with one existing
Starlette/AnyIO deprecation warning, in 673.42 seconds. Exit code zero.
Evidence: `backend/.cache/final-verified-suite/imports-97der4ml/pytest.txt`.
The complete suite ran in a disposable local PostgreSQL cluster, not the
development app database or production.
