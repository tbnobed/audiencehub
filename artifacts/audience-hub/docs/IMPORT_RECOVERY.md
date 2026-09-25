# Import recovery and worker coordination

## CLI load monitoring

`kinship load-status` takes a read-only snapshot; `kinship load-status --wait`
polls every two seconds and prints progress immediately and every 30 seconds,
then a final summary. `--timeout SECONDS` bounds the wait (default 7200).
Exit codes are **0 PASS**, **1 FAIL** (including timeout/empty selection), and
**2 PENDING** for a non-waiting snapshot with work still queued or running.
No workers are started, jobs retried, data repaired, or concurrency changed.

By default, the selection is **all imports present at invocation**, frozen for
the duration of the command, including historical failures and unsubmitted
uploads. Use repeatable `--import-id ID` to monitor a particular run and exclude
unrelated failures. Missing IDs, no imports, unsubmitted uploads, cancelled or
failed jobs, and orphaned running imports are explicit failures, not success.
Completed imports with no active jobs and no unresolved records are a valid PASS.
The newest job per import/type is authoritative after an explicit retry.

The monitor follows import-scoped child jobs and global identity work active
while the selected imports run (or queued during their execution window).
Identity jobs are globally deduplicated and do not carry import IDs, so a
captured shared resolver must finish even if it also serves another import.
Unresolved selected source records with no live resolver fail immediately.
Unrelated historical failed resolver jobs do not invalidate a completed load.
Queued work, including delayed retries and dependency-blocked jobs, is pending;
the timeout prevents absent workers or stuck dependencies from hanging forever.

The summary includes each import's accepted/rejected/warning counts and rows/s
(accepted plus rejected divided by persisted import duration; `n/a` if unknown),
database-wide profiles/merges/gifts, and total wall clock from the earliest
selected import start to final tracked completion (or now while incomplete).
The monitoring elapsed time is reported separately.

`kinship seed ... --load` automatically uses this same wait reporter **once**,
scoped to the five generated/reused import IDs, before post-load statistics and
trait computation. It retains `SEED_LOAD_TIMEOUT_SECONDS` as its timeout.
Generation without `--load` does not access the database. Monitoring never
generates another seed or changes the random seed.

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