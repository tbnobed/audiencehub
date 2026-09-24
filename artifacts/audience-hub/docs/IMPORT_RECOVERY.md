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

Contact, consent and enrichment imports hold a shared advisory lock across batch
commits. Identity resolution takes the exclusive counterpart. A resolver that
finds an active conflicting import or cannot acquire the lock is deferred for
30 seconds without consuming a failure attempt. Overlapping import writes use
stable conflict-key ordering.

Worker ERROR logs include sanitized exception chains and stack frame locations.
SQLSTATE-aware failure messages also appear in `jobs.error` and the System page.
Raw SQL parameters, source lines, local variables and arbitrary input-bearing
exception text are deliberately excluded. Failures recorded before this change
cannot have their missing exception information reconstructed.

The guarded `python -m app.cli reset-demo --yes` command is destructive; see the
README for its retained configuration, production guard and exact scope. It is
not required to retry an import.