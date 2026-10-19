# HubSpot source and destination

HubSpot is TBN's email platform. Its CRM contact ID (`hs_object_id`) is
Kinship's existing `hubspot_record_id` identity key. The integration does not
change the Master ID resolver, seed data, identity key proportions, or audiences.

## Connection and scopes

Create a source of type **HubSpot**, open **Connection**, and save a HubSpot
private app access token. Destinations have their own write-only token field.
Tokens use the existing Fernet encryption and stable `FERNET_KEY`; GET responses,
run history, audits, errors, and token inspection URLs never contain the token.
Leaving the token field blank preserves the saved token.

**Test connection** reads the HubSpot account/app IDs and the token's granted
scopes using POST `/oauth/v2/private-apps/get/access-token-info`. It lists missing
scopes separately for each feature:

| Feature | Scopes |
|---|---|
| Contacts | `crm.objects.contacts.read` |
| Property discovery | `crm.schemas.contacts.read` |
| Subscription preferences | `communication_preferences.read` (or `communication_preferences.read_write`; enterprise batch reading also supports `communication_preferences.statuses.batch.read`) |
| Optional marketing email engagement | `content` |
| Master ID property creation/read/write | `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.schemas.contacts.read`, `crm.schemas.contacts.write` |
| Static lists | `crm.lists.read`, `crm.lists.write` |

A scope shown for an unused feature is not required to use the other features.
Only contacts, subscription reading, and optionally engagement are required for
a source pull. Property discovery needs its additional scope. Destinations need
the Master ID and list scopes.

HubSpot IDs are portal-local. Pulls and destinations reject a different portal
once an inbound portal is established, rather than changing identity keys or
joining unrelated contacts on coincidentally equal numeric IDs.

## Inbound

Select CRM properties and map their columns to canonical fields. Suggested
mappings cover names, address, email and real identity keys. `hs_object_id`
always maps to `hubspot_record_id`, regardless of editable mapping; its contact
record ID is retained as source provenance, not a new identity key. The native
HubSpot email is retained separately for subscription matching, even when an
administrator maps a different property to the canonical email.

Preview reads at most **100 mapped contacts** without importing them. Run queues
the existing source/import pipeline. CRM v3 search uses `lastmodifieddate` with a
fixed upper watermark, a two-minute indexing delay, and a five-minute overlap.
ID keyset pages avoid the CRM search API's 10,000-result pagination ceiling.
Actual contact properties are fetched through batches of at most 100.

Contact pages and optional engagement pages have durable manifests and
checkpoints. All files enter one existing import/identity batch. Retrying a
copied batch uses the existing migration receipt, never silently discards
partially committed work, and does not import it again.

Subscription preferences are refreshed for **all retained contacts from this
source**, including contacts whose CRM properties did not change. Preferences
are not assumed to share the CRM modification cursor. Enterprise batch scopes
allow 100-email requests; other tokens use the per-email preferences API. Large
non-enterprise subscription sweeps therefore take substantially longer and must
be scheduled with that API budget in mind.

Each preference retains the subscription type, decision and provider timestamp
in consent evidence with platform `hubspot` and the HubSpot source ID. A global
CRM opt-out is recorded as observed evidence, not a fabricated historical date.
Any known unsubscribe creates a peppered email suppression immediately; consent
application then uses the existing manual/authoritative-source precedence.
Non-authoritative opt-ins cannot overwrite an opt-out. Suppressions are not
automatically removed by a later import; review an intentional resubscribe
through the existing consent/suppression administration.

Email suppressions apply to every activation exporting an email identifier,
including CSV, webhooks, SFTP and advertising exports, even if no consent channel
was explicitly selected. Master-ID-only metadata writes are not marketing sends.

The optional engagement toggle imports sent/opened/clicked/bounced email events
with stable provider event IDs and the same bounded, checkpointed window.
Identity resolution is not changed to process these event rows.

The contact watermark advances only after import, identity resolution, and the
checkpointed subscription refresh complete. Trait refresh is queued through the
existing pipeline. Parent source jobs remain pending while subscription refresh
runs, so the source history does not report an early success.

## Outbound

Create a **HubSpot** destination, select a segment and a dedicated static list
name (or existing numeric list ID), save its token, and test the connection.
Preview shows up to 100 locally mapped, consent-eligible contacts.

**Use a dedicated MANUAL contact list.** Each sync mirrors the segment's eligible
contacts: it removes contacts outside the consented segment, including manually
added members. Dynamic/active lists and non-contact lists are rejected.
The created list ID is saved without overwriting concurrent configuration edits,
so a later name edit renames the same list instead of abandoning its membership.

For contacts in the selected segment, the sender ensures the writable string
property `kinship_master_id` exists. It batch-reads existing values and updates
only incorrect/missing values in batches of 100. Identity metadata can be
corrected even for a contact that is excluded from marketing list membership.
Ambiguous ownership of a HubSpot ID by different active Master IDs fails closed.

List membership always requires an email opt-in in Kinship. Suppressed emails,
missing emails, and HubSpot global opt-outs are excluded. Suppression/consent is
checked again before additions. Membership updates use the CRM v3 list API in
batches of 1,000, removing excluded members before adding new ones and performing
a final consent cleanup. Multiple HubSpot contacts belonging to one Kinship
profile each receive the canonical Master ID.

Schedules use the same hourly/daily/monthly window receipts as existing source
jobs, so a scheduler tick cannot repeat a completed time window. Manual and
scheduled destination runs use ordinary activation snapshots, funnel counts,
history, and failed-run alerts. Expanded run history reports list ID, members
added/removed, Master IDs updated/unchanged. Profile funnel counts remain profile
counts; the extra `contacts_synced` metric counts native HubSpot contacts.

Network errors, 429s, and 5xx responses use bounded backoff, `Retry-After` and
HubSpot rate/quota headers. Partial/incomplete batches do not advance the cursor.
Errors are sanitized. An interrupted outbound run may have already changed the
remote system; it is recorded as failed, not automatically replayed as a success.
An explicit new run reads actual remote membership and reconciles it again.

Staged contact files participate in privacy erasure; revoked pulls cannot be
replayed. Active HubSpot pulls/finishers are a deletion barrier like existing
imports. Upload retention also expires inactive staging checkpoints, including
failed pre-import pulls. Expired incomplete checkpoints are abandoned without
removing committed import data; a new run starts from the previous completed
watermark rather than trying to replay expired files.

## CLI

```sh
kinship sources test SOURCE_ID
kinship sources preview SOURCE_ID
kinship sources run SOURCE_ID
kinship destinations run DESTINATION_ID
```

Run commands queue work and return its job/run ID; they do not embed credentials
in command arguments. Configure the token in the administration UI.

## Production verification

This change is code-only. No tests, builds, seeds, imports or benchmarks were
executed in Replit. The migration adds checkpoint storage and activation result
metadata; apply it through the normal server migration/startup procedure.

Added backend and frontend regression definitions are unexecuted here. On the
production verification environment, also check a limited test segment against a
dedicated HubSpot list: unchanged-contact unsubscribe, partial-run resume, token
rotation, already-correct Master IDs, membership removal, schedule deduplication,
quota backoff, and erasure of staged contact files.
