# Snowflake sources and Zeta exit migrations

These are **administrator UI workflows**. The server CLI is an equivalent
entry point, not a prerequisite. Apply the normal migration chain through
`0039_zeta_migrations` when deploying this change.

## Snowflake

1. Open **Sources → Add source → Snowflake**, then configure the source.
2. Enter the account identifier, user, SELECT-only role, warehouse, database,
   schema, PEM private key and optional passphrase. **Test connection** checks
   `CURRENT_VERSION()` and lists accessible tables in the selected schema.
   Authentication, network-policy, role and warehouse errors are sanitized.
3. Save the connection. PEM/passphrase are write-only and Fernet encrypted on
   the server. GET responses contain only the configured flag and nonsecret
   connection fields. Secrets are not retained in browser mutation caches.
4. Add each table or read-only SQL mapping. Targets: people, donation,
   donation_return, order_shipped, vod_viewed, source_codes, products, vod_titles,
   model_scores. Table columns auto-match Zeta names; column mappings remain
   editable. Unrecognized values are retained as raw attributes/properties.
5. Choose an incremental cursor, if needed. Boundary ties are replayed
   inclusively and stable record/message IDs make them idempotent.
6. **Preview** shows up to 100 mapped rows, found identity keys and validation
   totals. It neither imports rows nor advances a cursor.
7. Set each mapping to manual, hourly, daily at a selected time, or monthly
   (days 1–28), in America/New_York. Editing only its schedule preserves its
   cursor; changing its transformation resets only that mapping's cursor.
8. **Run now** queues a worker. **Imports** shows collection progress before
   COPY imports are registered, then committed/rejected row and import counts.
   The source's pull history remains available.

The connector consumes Snowflake result batches/iterators, never `fetchall()`
or a dataframe. COPY preparation is chunked; committed COPY batches and
receipts survive worker failure. Cursor advancement occurs only after every
prepared import succeeds. SQL is constrained to the existing SELECT grammar;
DDL, DML, multi-statements and SQL comments are rejected.

Server equivalents (these queue work on the normal worker):

```sh
kinship sources test SOURCE_ID
kinship sources preview SOURCE_ID MAPPING_ID
kinship sources run SOURCE_ID MAPPING_ID
```

## Zeta exit migration

1. Create a Zeta exit/SFTP source in **Sources**. For browser-only imports,
   no remote credentials are required. For the drop folder, configure the
   existing pinned-host-key SFTP connection first.
2. Open **Imports → Import Zeta export** and select that source.
3. Enter the real Zeta counts for **all 23 audiences**. The catalog values
   initially displayed are reference values, not measured live Zeta counts.
4. Create the migration session. Upload people JSONL/CSV, donation,
   donation_return, order_shipped and vod_viewed events; source_codes,
   products and vod_titles resources; model scores and identity lookup tables.
   HubSpot, Okta and donor-account lookups link to a Zeta UID.
5. File types are detected from filenames, headers and event names. A browser
   upload can explicitly select its type. Unknown columns are retained;
   people/model/lookup rows without a real `zeta_cdp_id` or `zeta_uid` are
   rejected. A generic source record ID does not invent a Zeta identity.
6. Browser uploads use 8 MiB streamed requests and durable offsets. After a
   disconnect or page reload, select the **same unchanged file** in the same
   session to resume. Changing a prepared file or target requires a new
   session; it must not silently rewrite a partially committed import.
7. Alternatively, select files from `/users/`, `/resources/` and
   `/events_non_real_time/` in the SFTP picker. Workers download bounded chunks
   with saved offsets. Publish immutable files by atomic rename; changing a
   remote file's size/mtime invalidates that download.
8. **Run migration** prepares checkpointed CSV manifests and uses the existing
   COPY importer and one identity batch. Event/gift rows only attach to
   resolved people; they never enter the identity resolver as people.
9. Watch the session's progress. After identity attachment **and the final
   traits/rollups job have succeeded**, parity counts all 23 current saved
   template definitions in one repeatable-read snapshot. The report shows
   Kinship count, user-entered Zeta count, difference and percentage.
   A zero Zeta denominator is displayed as no percentage, never infinity.
   Scope is all active Kinship profiles, not just one upload.
10. Counts can be corrected and parity re-run without importing again.

Real CDP/UID keys join people, models and lookup evidence. Their source record
envelopes remain distinct so model/lookup imports do not overwrite people's
raw fields. Source-local CDP/UID aliases support event attachment without
adding a generic `external` key to `identity_xref` or changing the resolver.
Native event message IDs take precedence over content-derived fallback IDs.
Financial returns reference their original namespaced ledger gift.

CLI equivalent:

```sh
kinship import-zeta /path/to/immutable/export-folder
kinship import-zeta /path/to/export-folder --source-id SOURCE_ID --counts real-counts.json
```

The CLI snapshots local files with bounded copying, registers a resumable
session and queues its worker; an SSH connection need not remain open for the
import. Without `--counts`, reference catalog counts are used and can be
replaced through the UI before re-running parity.

## Privacy and retention

Raw exit files are protected local uploads, not a second permanent archive.
They participate in privacy revocation and the existing completed-upload
retention policy. A privacy revocation can invalidate an incomplete upload;
start a new session rather than restoring revoked server bytes. Privacy waits
for active migration import jobs to release their data before erasure.
Completed imports retain the existing suppression and deletion checks.
Migration metadata and parity contain counts, never exported row contents or
connection secrets. Demo reset includes the new session/file tables.

## Production-only verification

**No tests, data imports, seeds, benchmarks, builds or server starts were
performed in Replit for this change.** Regression tests were added/updated but
not executed.

On the verification server check:

- Migration upgrade and full backend suite, including the new file/mapping
  regressions and the existing source pull/privacy suites.
- Admin access versus non-admin denial, and no PEM/passphrase in any GET,
  error, audit event or browser cache after save/test.
- Correct Snowflake version/table list, mapped row preview, cursor ties,
  batched results and hourly/daily/monthly per-mapping scheduling.
- Imports progress during collection and COPY; retry after an interrupted
  COPY resumes receipts without duplicate gifts or cursor loss.
- Upload interruption/reload resume and conflicting-offset rejection; reject
  oversized chunks, unsafe paths, missing Zeta IDs and mutable SFTP exports.
- All people/event/resource/model/lookup file types, unknown fields,
  independent envelopes, one master per connected identity and no event-only
  profile creation.
- Parity waits for final traits/rollups and reports all 23 user-entered counts.
- Privacy erases/revokes raw exit files as well as prepared import files;
  completed-file retention and demo-reset inventory include migration data.
