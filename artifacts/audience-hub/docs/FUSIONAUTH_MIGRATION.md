# TBN+ identity cutover: Okta to FusionAuth

This changes TBN+ **partner identity**, not Kinship console authentication.
Kinship console users remain local accounts. No FusionAuth service, SDK,
Replit integration or credentials are required by Kinship for this mapping.

## Upgrade and link existing partners

Deploy the code and apply the normal Alembic upgrade through
`0046_fusionauth_identity` before starting the updated API and worker.
Back up the database first. The migration preserves all Okta keys, profiles,
Master IDs and existing device links.

Prepare a UTF-8 CSV with these exact headers:

```csv
okta_customer_id,fusionauth_user_id
legacy-okta-id,new-fusionauth-id
```

Mount/copy that file onto the server's `/data` volume, then run:

```sh
docker compose exec -T api kinship fusionauth-map \
  /data/okta-fusionauth.csv --report /data/fusionauth-report.csv
```

Use a **new report filename on every run**; existing paths are never overwritten.
The command uses an exclusive identity/privacy coordination lock and temporary
COPY staging, then links unique existing Okta owners in one transaction.
It never invokes the person resolver, creates people, merges profiles, changes
subscriptions or edits audience definitions. It does not remove legacy Okta IDs.
If imports/resolution/deletion hold the lock, retry later; it fails explicitly
rather than racing them.

The CSV report contains every row with status:

- `linked`: added the FusionAuth key to the existing Master ID.
- `already_linked`: safe rerun; no extra key evidence is added.
- `unmatched`: no current, undeleted Okta owner; no new person is created.
- `ambiguous_okta`: multiple current owners; operator review required.
- `conflict`: contradictory mappings in the file, a FusionAuth ID already
  owned by somebody else, or another FusionAuth ID already on that person.
  All contradictory file rows are rejected, not whichever row happens to run last.
- `suppressed` / `invalid`: no link added.

The command prints counts, not identifier values. Report rows are provisional
until the command prints `"committed": true`. If disconnected during commit,
rerun the same mapping with a new report filename to reconcile safely.
An existing report is created private (`0600`), but contains identifying data.
Restrict access, review unmatched/conflict rows, and delete input/report files
after reconciliation under your retention policy. These operator files are
not database tables and cannot be erased by a profile deletion request.

## Dual-key operation

- `fusionauth_user_id` is a deterministic system key. One value may belong
  to only one person; database enforcement allows multiple source evidence
  rows for that same owner and reversible same-transaction merge moves.
- CSV auto-mapping, warehouse/source pulls, identity lookup/filedrop,
  standard contact resolution, transaction/viewing attachment, privacy
  redaction and suppression recognize both provider key types.
- The 23 audited templates use subscription/giving evidence, **not the login
  provider**. Keep those properties unchanged in replacement TBN+ feeds.
  Adding the mapping alone must not change any template membership.
- Custom login-key `exists`/null audience rules accept either provider.
  Exact value comparisons remain provider-specific: an old Okta literal
  must not be treated as if it were a FusionAuth ID.
- For viewing events, send `fusionauth_user_id` (or legacy `okta_customer_id`)
  in event `properties`; identify calls may send it in `traits`.
  Existing people are attached without creating people from viewing events.
  Stored unattached events with login IDs can attach on normal ingestion
  backfill after the mapping becomes available.
- The signed, server-to-server `/v1/device-events/{source_id}` channel
  accepts either top-level key. If both are supplied, both must uniquely
  resolve to the same current person. Public SDK input still cannot
  authenticate device links, and device identifiers never merge people.
- Data Health shows current-person key coverage including explicit zeros,
  and TBN+ total, Okta, FusionAuth, both, Okta-only, FusionAuth-only and
  neither. The TBN+ cohort includes a login key **or** non-empty subscription
  evidence; this also exposes subscription records lacking both keys.

Keep upstream TBN+ subscription, source priority and event properties unchanged
while replacing the login ID. This is not an automatic Okta API migration or a
replacement for FusionAuth's own account/password migration.

## Production-server verification only

No tests, seeds, imports or benchmarks were run in Replit.
New disposable-schema integration cases are in `tests/test_fusionauth_pg.py`.
Run them against a dedicated disposable database, never the production dataset:

```sh
docker compose exec -T -e CONSENT_TEST_DATABASE_URL api \
  python -m pytest tests/test_fusionauth_pg.py
```

Supply the disposable database URL using your server's environment/secrets
mechanism. The fixture creates and drops random schemas.
Before/after the real mapping, compare total profiles, Master IDs, merges and
all 23 audience memberships. Verify a known linked login by both key types,
repeat the mapping, inspect unmatched/conflicting/suppressed rows, and verify
FusionAuth-only contact ingestion, signed device links and viewing attachment.
Confirm Data Health's both/only/neither totals reconcile to its TBN+ cohort.
The added cases also check deferred database ownership enforcement, private
reports, unchanged audience memberships and no new people during mapping.
