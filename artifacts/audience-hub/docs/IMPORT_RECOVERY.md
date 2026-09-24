# Import recovery and worker coordination

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