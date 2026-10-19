# Audited template activation and refresh

New audited templates are active, owned by the first admin (lowest user ID), and
scheduled daily using the existing 04:00 UTC scheduler. `reset-demo` recreates the
23 templates in its reset transaction; their counts are populated after loading.
`kinship seed --load` materializes all 23 after identity, traits and snapshots,
using the seed's reference date. This fills Partners, Change and Last refreshed.
Normal API startup preserves existing edits, pauses and archives.

To enable the already-loaded production dataset without resetting or reseeding:

```sh
docker compose exec -T api kinship segments activate --all-templates
docker compose exec -T api kinship segments refresh --all
```

Activation queues refreshes as well as setting daily schedules. Both commands
are safe to rerun: pending jobs for the same audience revision are reused.
`refresh --all` refreshes active audiences only; drafts/paused and archived
audiences are skipped. Commands enqueue work; the worker must be running.

The Audiences table supports selecting individual or all visible non-archived
audiences, then **Activate**, **Pause**, or **Refresh now**. Pause uses the existing
draft state and retains membership/count history. Already queued jobs skip paused
or archived audiences. Refresh requires an active selection. Dependency checks
reject pausing a referenced audience without also selecting its active dependents;
bulk changes are transactional. Analyst/admin access is required.

Regression definitions cover defaults, reset recreation, pending-job deduplication,
dependency-aware bulk actions, retained counts, and seed finalization. They were
not run in Replit; runtime verification is reserved for the production server.
