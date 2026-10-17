# Import recovery and worker coordination

## CLI load monitoring

`kinship load-status` takes a read-only snapshot; `kinship load-status --wait`
polls every two seconds and prints progress immediately and every 30 seconds,
then a final summary. `--timeout SECONDS` bounds monitoring (default **14400,
four hours**), not the running work.
Exit codes are **0 PASS**, **1 FAIL** (confirmed failure or empty selection),
**2 PENDING** (non-waiting snapshot), **3 STILL RUNNING** (timeout with fresh
heartbeats on all tracked active workers), and **4 UNKNOWN** (timeout with
queued/stale work, missing completion evidence, or unknown state).
Heartbeat freshness establishes worker liveness, not advancing data counters.
Timeout is never reported as a failed load and never cancels jobs.
No workers are started, jobs retried, data repaired, or concurrency changed.

By default, the selection is **all imports present at invocation**, frozen for
the duration of the command, including historical failures and unsubmitted
uploads. Use repeatable `--import-id ID` to monitor a particular run and exclude
unrelated failures. No imports, unsubmitted uploads, cancelled or failed jobs
are explicit failures. Missing IDs, orphaned running imports and unresolved
contacts without an active resolver are UNKNOWN, never a false PASS or FAIL.
Completed legacy imports with no active jobs and no unresolved contacts can PASS.
Durable identity batches additionally require a succeeded, batch-scoped
traits job: the identity batch's `completed` phase means attachment finished,
not that traits and dashboard rollups finished.
The newest job per import/type is authoritative after an explicit retry.

The monitor follows import-scoped and batch-scoped child jobs. For older jobs
without scope, it follows global identity work active while the selected imports
run (or queued during their execution window), and follows the resolver's linked
traits job. A captured shared resolver must finish even if it serves other imports.
Unrelated historical failed resolver jobs do not invalidate a completed load.
Queued work, including delayed retries and dependency-blocked jobs, is pending;
the timeout prevents absent workers or stuck dependencies from hanging forever.

The summary separates **selected unresolved_contacts** (contact/enrichment/consent
source records, excluding gift/event records) from **global unattached_gifts**
and **global unattached_events**. Unattached transactions may be genuinely
unmatchable; their presence alone neither proves unfinished identity nor fails
the load. These attachment metrics are database-wide, not selected-import counts.
The previous `unresolved=6589171` production number included gift/event source
records and was not a valid contact identity backlog.

It reports the current importing/resolving/attaching/traits/rollups phase and
persisted per-phase elapsed **current-attempt job-seconds** (overlapping workers
may sum to more than wall time). Import execution durations use actual job
start/finish timestamps when phase telemetry is absent. Retry attempts reset
open phase timers, phase counters and attachment-step details: stopped-workspace
or retry downtime is never added to the next attempt. A bounded history retains
earlier closed counters separately, not in current totals. Older phase telemetry
without attempt ownership explicitly shows `unknown/not-recorded`.
Small job/barrier reads run every poll. Exact counts are cached for five minutes,
labelled with their age, and refreshed before PASS; no repeated large joins run
every two seconds. Metric statements have a 15-second limit; unavailable final
metrics yield UNKNOWN, not PASS.

The summary also includes each import's accepted/rejected/warning counts and rows/s
(accepted plus rejected divided by persisted import duration; `n/a` if unknown),
database-wide profiles/merges/gifts, and total wall clock from the earliest
selected import start to final tracked completion (or now while incomplete).
The monitoring elapsed time is reported separately.

`kinship seed ... --load` automatically uses this same wait reporter **once**,
scoped to the five generated/reused import IDs, before post-load statistics and
trait computation. `SEED_LOAD_TIMEOUT_SECONDS` controls the seed loader's timeout.
Generation without `--load` does not access the database. Monitoring never
generates another seed or changes the random seed.

### Resume monitoring, not data generation

Run from the backend directory
`/home/runner/workspace/artifacts/audience-hub/backend` for this checkout, or from
your deployment's backend/container working directory. Preserve the exact IDs
printed in the timeout summary; do **not** generate another seed to resume waiting:

```sh
python -m app.cli load-status --wait --timeout 14400 \
  --import-id 1 --import-id 2 --import-id 3 --import-id 4 --import-id 5
```

For Docker Compose deployments, prefix this with `docker compose exec api`.
The IDs above are the five imports from the attached production log; use your
actual IDs for another run. Exit 3 means leave workers running and resume this
read-only monitor. Exit 4 means inspect worker health and job/barrier state before
deciding whether recovery is needed; it is not permission to reset or rerun data.

Implementation and isolated tests:
`/home/runner/workspace/artifacts/audience-hub/backend/app/load_status.py`,
`/home/runner/workspace/artifacts/audience-hub/backend/app/jobs/phase_progress.py`,
`/home/runner/workspace/artifacts/audience-hub/backend/tests/test_load_status.py`.
The tests create and remove a private PostgreSQL cluster; they never write to the
application database.

Imports commit each 10,000-row batch, including the record cursor, cumulative
counters and diagnostic entries. A failed import therefore retains its previously
committed records. Use **Resume Import** in the failed-import dialog to continue;
do not remap a partially committed import. Mapping changes and preview validation
are rejected after the first committed batch.

The checkpoint is a CSV record number, not a physical line number. Quoted
multiline fields do not change resume boundaries. Whole-file hard-bounce
precedence is preserved before writing, including on retry.

Migration `0008_import_checkpoints` adds the checkpoint and diagnostic storage.
Rebuild the API and worker images together; normal API startup applies migrations.
Stop old workers before upgrading so old and new transaction behavior do not mix.

Import writes hold a shared transaction advisory lock for one batch only.
Identity resolution takes the exclusive counterpart for groups of at most 500
records, releasing it on each commit. Validation holds neither advisory nor
import-row locks. A resolver that cannot acquire the coordination lock is
deferred for 30 seconds without consuming a failure attempt; a running import
does not exclude resolution between its batches. Identifier locks use 1,024
distinct, sorted hash buckets instead of one lock per identifier. Overlapping
import writes use stable conflict-key ordering.

Worker ERROR logs include sanitized exception chains and stack frame locations.
SQLSTATE-aware failure messages also appear in `jobs.error` and the System page.
Raw SQL parameters, source lines, local variables and arbitrary input-bearing
exception text are deliberately excluded. Failures recorded before this change
cannot have their missing exception information reconstructed.

The guarded `python -m app.cli reset-demo --yes` command is destructive; see the
README for its retained configuration, production guard and exact scope. It is
not required to retry an import.