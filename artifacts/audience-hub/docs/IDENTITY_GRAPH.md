# First-party identity graph

This change does not change deterministic resolver rules, the nine original
identity key types, seed proportions, Master ID issuance, winner ordering or
retired-ID behavior. Devices and households are separate relations, not inputs
to the deterministic connected-components resolver.

## Deployment

Build the API image and recreate API/worker together. Migration
`0044_first_party_graph` adds evidence, address, household, device, review and
history storage and refreshes profile-backed views. Dependencies are pinned in
the Python manifests and hashed production lockfile: Splink 5 and usaddress.
No Replit service is required.

Probabilistic scheduling is **disabled by default**. Admin → Identity quality
exposes thresholds, schedule hour (UTC), device lifetime, surname policy, manual
and dry-run batches, run history, sampling and review verdicts. Start with a dry
run and inspect samples before enabling scheduled automatic links.

## Evidence and reversal

Every crosswalk row has `method`, `confidence`, `rule`, source and first/last seen
timestamps. Existing edges are explicitly classified as deterministic, confidence
1, with their existing resolver as the rule; no retrospective model score is
invented. The profile Identity tab explains each key's provenance.

Probabilistic links use the **existing pre-move merge journal**. Both sides'
crosswalk metadata is captured before annotations, alongside identifiers,
source records, gifts, events, consent and enrichment. Original winner edges
keep their evidence. New edges from the other side identify the probabilistic
bridge. Unmerge restores per-side evidence, preserves opt-outs, retires IDs in
the same manner as before and allocates a new Master ID to the separated side.
It still refuses changed source records or later dependent merges.

## Address normalization and households

usaddress parses street number, directional/street/suffix, unit, city, state and
ZIP+4. Normalization includes state names, common street suffixes and directional
abbreviations. A stable SHA-256 address key includes street number, street, unit,
city, state and ZIP5; ZIP+4 is retained separately. Different apartment numbers
are never treated as the same address. Incomplete, ambiguous, PO-box and foreign
addresses do not get a US street-address grouping key.

Parsing follows canonical fields after the trait pipeline and daily maintenance;
unchanged address inputs keep their parser/vendor result. Household IDs are
stable hashes of normalized address plus surname (surname restriction defaults
on and is configurable). Null/incomplete addresses remain independent people.
Household membership **never merges profiles**. Profile → Identity shows members;
`profile.household_id` and `profile.household_size` are available in audience rules.

An administrator can ingest vendor CASS/NCOA results using
`POST /api/identity/graph/addresses/vendor` (CSRF-protected multipart file).
CSV columns:

```
profile_id,input_hash,vendor,number,street,unit,city,state,zip5,zip4
```

The adapter preserves vendor evidence without a schema change. `input_hash`
must match the current address input; stale results are counted, not applied.
Files are processed in memory, limited to 10 MiB, and not stored. Additional
vendor adapters can call `identity.addresses.vendor_result` with the same contract.
This is an import hook, **not** an in-house CASS certification or NCOA service.

Every activation has a **One per household** checkbox. All destination types
(CSV, Google, Meta, webhook, SFTP including crosswalk, and HubSpot) use the shared
filtered export work set. One stable representative is chosen **after** consent,
suppression and required-field filtering, ordered by Master ID then profile ID.
Unknown addresses stay independent. Excluded household members are counted in
the run funnel; delta removals are based on the final representative set.

## Probabilistic batches

Splink runs directly on PostgreSQL using a per-run scratch schema, not a pandas
download or an external scoring service. Blocking is the union of:

1. Same normalized address.
2. Same normalized phone.
3. Same last name and ZIP5.

Comparisons: first name (including the versioned nickname table), last name,
normalized address, DOB and phone. Missing values are null levels, not matches.
The versioned Fellegi–Sunter model uses explicit conservative m/u parameters and
a match prior. These are initial model assumptions, **not a measured accuracy
claim**. Use sampled verdicts to evaluate the production population before
enabling automatic linking or changing the model.

Scores at/above the upper threshold are auto-link candidates; scores at/above
the review threshold and below the upper threshold enter review; lower scores
do not link. Defaults: review `.85`, auto `.995`.

**Any differing value of any of the five system key types blocks a probabilistic
merge**, including a human approval. Conflicts go to review instead. Scores from
a now-retired profile are not reused transitively. Human decisions are not
overwritten by a scheduled batch. Manual `not_match` on a probabilistic linked
pair uses guarded unmerge, not an unsafe record split.

The worker holds the existing resolver/privacy/traits barriers. Batch writes
are transactional; no partial graph merges are committed on failure. Run history
is durable separately. Jobs use the existing cancellation/ownership fencing;
scratch connections are tagged as part of the same job. Scratch schemas are
dropped after a run; approved erasure also removes scratch left by a killed job.

A dry run writes only its run/job/audit history and isolated temporary scoring
data (removed afterwards). It does not change profiles, crosswalks, addresses,
households, device links, reviews or audience memberships.

PostgreSQL role requirements: create/drop the isolated scratch schema, install
the `fuzzystrmatch` extension if not already present, and create Splink's SQL
functions in that schema. Provision the extension with the database owner if
the application role cannot install it. No browser or runtime dependency on
DuckDB extensions is used: all input tables are registered as PostgreSQL tables.

Server CLI:

```sh
docker compose exec -T api kinship identity-graph run --dry-run
docker compose exec -T api kinship identity-graph status
docker compose exec -T api kinship identity-graph run --apply
docker compose exec -T api kinship identity-graph households
```

## Authenticated device observations

Supported device key types: `roku_rida`, `samsung_tifa`, `apple_idfa`,
`google_aaid`, `app_install_id`, `web_cookie_id`. Advertising IDs must be valid
nonzero UUIDs. Devices do not appear in person-resolution key sets.

An event can link a device only after the **originating trusted server** has
authenticated the person and includes their `okta_customer_id`. A JSON
`authenticated: true` flag, a web cookie, a public ingestion write key or
possession of a device ID is not proof of person authentication.

Generate a separate private, source-specific device event signing key:

```sh
docker compose exec -T api kinship identity-graph device-key --source-id SOURCE_ID
```

Store the one-time output **only in the trusted originating server's secrets**,
never in browser/mobile code or analytics payloads. Alternatively, an admin can
use CSRF-protected `POST /api/identity/graph/device-sources/{id}/rotate`.
The stored key is Fernet-encrypted and included in offline key rotation.
Rotating immediately invalidates the prior signing key.

POST `/v1/device-events/{source_id}` with JSON:

```json
{
  "event_id": "unique-origin-event-id",
  "occurred_at": "2026-10-20T12:00:00Z",
  "okta_customer_id": "existing-okta-id",
  "devices": {"web_cookie_id": "origin-cookie-id"}
}
```

Headers:

* `X-Kinship-Timestamp`: current Unix timestamp in seconds.
* `X-Kinship-Signature`: lowercase hex HMAC-SHA256 of
  `source_id + "." + timestamp + "." + exact_raw_request_bytes`, using the private
  source key. Signature must match the exact bytes sent.

Request time tolerance is five minutes; event IDs deduplicate delivery. Events
require a unique active Okta owner; a missing/unresolved owner returns 409, so
the originating server may retry after contact resolution. No identity
resolution retry loop is created. Events older than the configured device
lifetime or over five minutes into the future do not create links.

The same device can have authenticated observations for different people
(shared devices). Those people stay separate. Lifetime defaults to 90 days
since the latest authenticated event, configurable in policy. Late observations
cannot move last-seen backwards. Expired links disappear from profile reads and
are deleted by daily maintenance. New TTL policy also applies to existing links.
Okta ownership is reconciled after merges/unmerges without ever merging people
because of a device.

## Quality and production verification

Admin → Identity quality samples N linked pairs for manual review, including
deterministic linked source-record pairs and probabilistic merges. Verdicts,
reviewer, time and optional note are stored. `unsure` can be revisited. Data
Health shows daily per-source candidate/link rate, review arrivals, open queue
and sampled-link precision (correct / reviewed); missing samples show no
precision rather than an invented score.

Implementation was **code-only**. No tests, application starts, seeds, imports
or benchmarks were run in Replit. Static syntax checks are not runtime evidence.
Before enabling scheduled linking, verify on the server using a disposable
database/schema, not the 500k production dataset:

* Migration chain refreshes active_profiles and preserves filters/indexes.
* Existing deterministic resolver, Master ID, merge/unmerge suites still pass.
* `tests/test_first_party_graph_pg.py` covers address/unit safety, surnames,
  unchanged Master IDs, all system-key conflicts, probabilistic evidence and
  unmerge, authenticated-only devices, shared devices, expiry, segment fields,
  model comparisons, stale vendor results and privacy inventory.
* Exercise a full Splink dry run; compare every live-table count/checksum before
  and after, excluding jobs/run history/audit. Confirm scratch schema removal.
* Exercise all three score bands, conflicting system keys, decisions after
  source changes and reversal of dependent probabilistic merges.
* Exercise signed device requests: missing/invalid signature, old timestamp,
  unknown Okta ID, duplicate delivery, shared device and expired observation.
* Check one-per-household exports across each destination after consent and
  suppression exclusions, including delta removals and funnel reconciliation.
* Approve a deletion for a linked/device-bearing profile, then query **every
  table**, including new graph tables and any abandoned scratch schemas, for
  erased profile IDs/keys. Neither review notes nor merge snapshots may survive.
