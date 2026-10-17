# M7 verification — activation, consent and deletion

M7 is implemented before M6, per the requested milestone order. This report
records correctness checks on the combined working tree, not production-scale
acceptance or a claim that browser interactions have been verified.

## Combined verification results

One complete backend run used a newly initialized, disposable PostgreSQL cluster:

```sh
cd artifacts/audience-hub/backend
APP_ENV=development KINSHIP_BENCHMARK_BACKEND=local python -m app.benchmark \
  --imports --test-suite --output-dir ../../../.cache/m7-full-verification
```

**438 passed, 1 skipped, 1 warning in 274.59 seconds.** All 439 tests present at
that combined-run baseline were collected. The sole skip was
`tests/test_identity_scale.py::test_identity_scale_50000_default_lock_budget_concurrent_contact_import`,
which explicitly requires `KINSHIP_IDENTITY_SCALE_TEST=1`. That opt-in scale test
was not enabled. The warning is Starlette's deprecated AnyIO `BlockingPortal`
alias, not a failing application assertion.

No live application schema or production data was used. Webhook tests sent only
synthetic payloads to local test HTTP servers. Private cluster, fixture and log
artifacts were removed after recording these results.

Frontend commands, from `artifacts/audience-hub`:

| Command | Result |
| --- | --- |
| `pnpm build` | Passed; Vite transformed 2,808 modules and built in 7.26 seconds. |
| `pnpm lint` | Passed; this project's lint command runs TypeScript with `--noEmit`. |
| `node --test src/lib/api.test.ts src/components/activations/logic.test.ts` | 14 passed. |
| `pnpm test:profiles` | 125 passed; actual query/router/Radix rendering against the existing captured profile fixtures, including the 50-profile regression, in admin and viewer roles. |

The profile-render harness initially failed because its CommonJS VM substituted
only `import.meta.env.DEV`, while the shared API helper now also reads
`import.meta.env.BASE_URL`. Its Vite-environment substitution was corrected to
provide both values; no profile fixture or privacy behavior was weakened.
The complete 125-test profile render run then passed.

Non-blocking frontend warnings: the production JavaScript bundle is about
1,200 kB minified (343 kB gzip), above Vite's 500 kB advisory threshold, and the
renderer reports React Router v7 future-flag notices. Browser verification of
the signed-in activation and privacy workflows remains a separate integration
check; these automated results do not substitute for it.

## Required activation-test matrix

All test references below are relative to `backend/`. Every listed test was
included in the complete successful backend run above.

| Required behavior from `ACTIVATION.md` | Actual test evidence |
| --- | --- |
| Google normalized SHA-256 vectors, including uppercase/spaced email, punctuated phone, accents and apostrophes | `tests/test_activations.py::test_provider_vectors_and_formula_cells`; real file/header/hash assertions in `tests/test_activations_pg.py::test_real_run_each_destination_funnel_and_encryption[google_customer_match]`. |
| Meta normalized SHA-256 vectors and hashed partner external ID | `tests/test_activations.py::test_provider_vectors_and_formula_cells`; `tests/test_activations_pg.py::test_real_run_each_destination_funnel_and_encryption[meta_custom_audience]`. |
| Consent filtering and exclusion counts | `tests/test_activations_pg.py::test_real_run_each_destination_funnel_and_encryption` (all four destination parameters); `test_roles_ads_override_download_audit_and_retention` also checks admin override, analyst denial, and preserving an unchanged authorized override when pausing/resuming. |
| Suppression filtering and exclusion counts | `tests/test_activations_pg.py::test_real_run_each_destination_funnel_and_encryption` checks manual-email versus deletion suppression and missing-field counts across CSV, Google, Meta and webhook runs. |
| Webhook raw-body HMAC, retry and failure paths using a local server | `tests/test_activations.py::test_real_webhook_signature_retries_and_no_redirect`, `test_five_network_attempts`, and `test_slow_response_has_absolute_deadline`; `tests/test_activations_pg.py::test_delta_two_runs_failure_preserves_baseline_and_snapshot` checks partial success followed by failure. |
| SSRF: loopback, private ranges, metadata/link-local addresses, private DNS answers and explicit internal-network allowance | `tests/test_activations.py::test_ssrf_blocks_private_literals`, `test_ssrf_dns_all_answers_and_url_guards`, and `test_dns_pinned_connection_preserves_original_host`. Local-server signing/retry tests exercise the explicit internal-network allowance. |
| Correct added/removed membership across two delta runs | `tests/test_activations_pg.py::test_delta_two_runs_failure_preserves_baseline_and_snapshot` checks additions, removals, frozen queued configuration and preservation of the previous successful baseline on later failure. |

The canonical provider URLs, Unicode normalization decisions and five-total-
attempt retry interpretation are recorded in `ACTIVATION.md`.

### Post-baseline Meta mapping correction

Meta `external_id` is now explicitly validated as fixed to `profile.id` at both
API validation and execution. An explicit custom mapping, removed key or cleared
mapping receives a 422 response rather than being silently ignored. Omitting the
whole mapping when creating an activation still selects the provider preset.
The exporter no longer overwrites an arbitrary configured external-ID mapping.
Other provider fields remain configurable.

A separate disposable-PostgreSQL focused run after this correction passed
**7 tests, 435 deselected, in 12.51 seconds**:

- `tests/test_activations_pg.py::test_meta_external_id_is_fixed_and_missing_mapping_uses_preset`
  checks rejection of custom/constant/empty/null/removed external IDs and
  acceptance of the default and explicit preset.
- `tests/test_activations_pg.py::test_other_provider_fields_honor_custom_mapping`
  checks real Google and Meta file hashes after remapping a non-fixed name field.
- `tests/test_activations_pg.py::test_real_run_each_destination_funnel_and_encryption`
  reruns all four destination parameters.

The full backend suite was not rerun after this targeted correction; the
438-pass result above remains the combined baseline, not a post-correction claim.

## Consent, suppression and deletion evidence

The following tests are in `backend/tests/test_privacy_pg.py` unless otherwise
specified.

| Required behavior | Actual test evidence |
| --- | --- |
| Consent precedence in both policy implementations and ingestion | `test_consent_policy`, `test_database_policy_matches_python`, `test_authoritative_import_and_bulk_precedence`, and `test_authoritative_bounded_merge_preserves_new_explicit_choice`. |
| Manual changes, notes, role checks and suppression | `test_manual_consent_notes_roles_and_suppression`. |
| Different-admin approval and idempotent execution | `test_two_person_approval_and_idempotency` and `test_approval_concurrent_deduplication`. |
| Recheck newly matched identities; erase request ciphertext | `test_request_rechecks_new_matches_and_erases_its_encrypted_identifier`. |
| **Deletion completeness: query every table afterwards** | `test_deletion_completeness_every_table_and_exports` invokes `assert_all_tables_erased` after execution. It enumerates physical tables, including event partitions, checks scalar/array/JSON semantic partner references, and verifies no erased survivor or merged-alias profile IDs remain. It additionally checks export-file removal, run snapshot erasure/revocation, suppression creation and retention of an unrelated event. Unrelated entity IDs are not confused with profile references solely because their numeric values match. |
| Fail closed for an unknown new profile-reference table | `test_unknown_new_profile_reference_fails_closed`. |
| Serialize privacy changes against readers; isolate lock namespaces | `test_privacy_shared_lock_and_schema_isolation`. |
| Defer deletion during active imports | `test_active_import_defers_deletion`. |
| Failed file revocation rolls back relational erasure and can retry | `test_file_revocation_failure_rolls_back_and_retry_succeeds`. |
| Prevent re-import through erased external-only aliases without blocking unrelated partners | `test_deletion_reimport_external_only_aliases_and_unaffected_partner`. |

Additional activation integration protection is covered by
`tests/test_activations_pg.py::test_write_ahead_erasure_index_and_filename_survive_worker_rollback`:
the member index and planned file path remain durable when the execution
transaction rolls back, while an incomplete run cannot be downloaded.
`test_real_job_dispatch_and_column_order` exercises the actual job dispatch path
and ordered-column persistence. `test_scheduled_dedupe_and_missing_traits_barrier`
and `tests/test_activations.py::test_cron_validation_timezones_and_dst` cover
scheduling/deduplication and trait readiness.

## Final integration and browser verification

The normal application workflow applied the migrations and was restarted for
the integrated implementation. Signed-in browser verification used only one
new synthetic partner and its own source, audience, destination and activation.
No production data was accessed.

- Admin A recorded email opt-in with a note, created the CSV destination and
  activation through the wizard, and ran it. All five funnel stages were 1;
  the CSV download succeeded.
- Adding a manual email suppression recorded opt-out. The next run selected
  1 and sent 0. Removing that suppression did not opt the partner back in;
  a separate explicit consent change was required.
- Admin A could not approve their own deletion. A distinct Admin B approved.
  After a workflow interruption, the worker automatically recovered the job
  following its configured 300-second stale-heartbeat threshold. The second
  attempt completed without a manual status change or direct relational erase.
- The deleted partner returned HTTP 404; its prior export returned HTTP 410.
  The completed request showed only counts and audit metadata. Email and
  external-ID tombstones were locked. Unrelated hard-bounce suppression remained.
- The test's activation and destination were deactivated and its audience
  archived. No unrelated partner or source was deleted.
- The final browser check exposed the synthetic email in the audience's
  description. The fix now redacts known aliases in metadata, freezes affected
  audience rules and their dependents to a valid empty definition, pauses
  affected activations, and revokes historical snapshots/exports as needed.
  A guarded, audited metadata-only repair of the already-completed synthetic
  request left **zero metadata matches**, confirmed in a new database transaction.
  That last correction was verified by focused tests and database checks,
  not by repeating the entire browser journey.

Additional focused verification after the 438-pass combined baseline:

- Historical-file deletion and safety: 32 privacy tests passed.
- Development identity/key stability: 24 tests passed, including actual
  two-admin API approval and persistent-key derivation using synthetic inputs.
- Scan fast paths, configuration-error redaction and recovery: 5 tests passed.
- Metadata erasure integrated with privacy: 37 tests passed; after extending
  address/diagnostic coverage, all 4 metadata-specific tests passed again.
- Final activation UI logic/copy: 13 tests passed; TypeScript checking passed.

These runs overlap; they are not additive suite totals. The complete backend
suite was not rerun after every targeted correction. Production-scale timing
and production deployment acceptance remain separate.

## Limits and remaining acceptance checks

- M5 remains accepted pending production-scale checks. This suite does not
  demonstrate 500k-partner performance, production concurrency, realistic
  destination throughput or production retention/restore procedures.
- Google Customer Match and Meta Custom Audiences produce manual-upload CSVs.
  No Google/Meta API synchronization or real external delivery was performed.
- The application cannot erase copies already downloaded or delivered to
  external recipients; downstream retention/deletion obligations remain an
  operator/provider responsibility.
- Local retry/SSRF tests verify implementation behavior, not the DNS, egress,
  TLS or network configuration of a future production deployment.
- Historical uploads without complete per-version provenance are revoked
  conservatively for affected sources. Other retained sources are scanned for
  matching aliases. This can take time; the request remains approved/in progress
  until erasure commits. Files can contain other partners whose database records
  remain intact. The Admin approval screen explicitly warns about this.
- Authentication identities and credential material are deliberately not
  rewritten by free-text metadata redaction. See SECURITY_PRIVACY.md for scope.
