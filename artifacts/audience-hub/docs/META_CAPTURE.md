# Meta first-party capture

Self-hosted integration: the API/worker talk directly to Meta's Graph/Marketing
APIs. No Replit connector, Replit runtime or Meta Pixel is required. No test,
seed, import, build or benchmark was executed in Replit for this change.
Migration `0045_meta_capture` is required; rebuild API and worker together.

## Connect a Page

1. Configure a Meta Business app, Facebook Login and Lead Ads webhooks in Meta's
   dashboard. Grant the Page access token `leads_retrieval`,
   `pages_manage_metadata` and the Page read permissions required by your app.
   Production apps need Meta app review/advanced access and appropriate Page
   leads access/CRM assignment. A token alone does not bypass those grants.
2. Sources → create **Meta Lead Ads** → Connection. Enter Page ID, app ID,
   pinned Graph version (default `v25.0`), Page access token, app secret and a
   random webhook verification token of at least 24 characters.
3. Save. Tokens/secrets are Fernet-encrypted, never returned, and included in
   the existing offline Fernet rotation procedure. Leaving a secret input blank
   preserves the old value.
4. Configure Meta's Page webhook callback to the public HTTPS origin plus the
   displayed `/v1/meta/webhook/{source_id}` path, with the saved verify token.
   Kinship answers Meta's verification challenge. POST bodies require the
   `X-Hub-Signature-256` HMAC of the exact request bytes, using the app secret.
   A public ingestion write key cannot authenticate this callback.
5. Map form fields to canonical contact fields. Bind a consent checkbox to its
   **exact form ID**, field/key, exact displayed wording, channel and explicit
   affirmative values. The form list supplies IDs/names. Do not infer permission
   merely because someone submitted a form.
6. **Connect and backfill** verifies the token's app, Page and required scopes,
   subscribes the app to Page `leadgen`, then queues the first backfill (30 days
   by default, configurable 1–90). This subscribes the Page to an already
   configured app callback; it cannot configure the app dashboard for you.

Each callback queues a lead ID. The worker fetches the lead's original timestamp,
form fields and `custom_disclaimer_responses`, verifies form ownership against
the connected Page, records source name/form ID/form name, and passes the contact
through normal suppression and Master ID resolution. Contact/event/consent work
is checkpointed; retrying does not create duplicate people or lead events.
Rate-limit headers, provider transient/rate-limit errors and Retry-After apply
backoff. Only opaque paging cursors are followed, never token-bearing next URLs.

Consent evidence preserves wording, timestamp, checkbox value and form provenance.
If Graph supplies a different question label from the configured wording, the
decision is **unknown**, not an opt-in. Unchecked/missing evidence never grants
permission or silently clears an earlier decision. Existing manual/authoritative
consent precedence and HubSpot/global suppressions remain in effect. Consent on
one form is never inferred for another form. Configure a verified checkbox
before backfill if you need its historical consent captured.

After changing mappings/consent, save then **Run now** to reprocess recent leads.
Suppressed records stay suppressed; original capture times and precedence remain.
Run history and captured/queued/suppressed counts are in the source panel.
Failed jobs can be retried through existing job controls; Run now requeues recent
unfinished deliveries. Mapping changes also allow completed recent leads to be
reprocessed without changing source-record IDs.

## Facebook Login and Page-scoped IDs

`POST /v1/meta/login/{source_id}` accepts JSON `{"access_token":"..."}` after
**Facebook Login in the first-party app**. Kinship validates the token with
Facebook: valid status, configured app ID, USER type, subject, expiry/data-access
expiry, and `/me` subject equality. It retrieves the profile's approved email/name
fields from Graph, not arbitrary caller-supplied claims.

This is identity capture, **not Kinship console SSO**, and it does not sign the
person into an administrator account. Set `INGEST_CORS_ORIGINS` to approved
first-party browser origins if calling it cross-origin. The endpoint is rate
limited and returns only acceptance, not a console session or token.

* `meta_app_user_id` is an additional **system key**, stored as `app_id:user_id`.
* `meta_psid` is Page scoped, stored as `page_id:psid`. Optional signed Messenger
  `messages` delivery can capture a person-provided email/phone quick reply.
  Subscribe the Page/app to Messenger messages separately with the required
  Messenger permissions; the Lead Ads connect button requests only `leadgen`.
  A PSID alone, arbitrary message text or a public SDK trait cannot create a link.

Verified keys have private server-side source-record provenance. CSV fields,
SDK traits and arbitrary attributes named `meta_app_user_id`/`meta_psid` do not
enter the key graph. The PSID path additionally requires a normalized provided
email/phone on that observation. Original key types, deterministic rules and
Master ID issuance/survivor behavior are otherwise unchanged. Probabilistic
matching treats a conflicting app-user system key like every other system key.

## Click attribution

The event API accepts `fbclid`, `_fbp`, `_fbc` at top level, in `context` or in
event properties (bounded opaque strings). The JS SDK captures the URL click ID
and existing first-party cookies, retains click context for the browser session
and derives a stable `_fbc` value when appropriate. Opt-out/GPC is checked before
capture; opt-out clears the SDK's click-session storage.

Evidence is stored against the source-scoped anonymous session. On identify,
or signup/login/donation tracking with an explicitly provided email/phone,
normal identity resolution runs and a `Meta Attribution` event attaches the
evidence to the resolved profile/Master ID. A unique existing owner is required.
Attribution never enters identifiers/xref and cannot merge two people.
Suppression includes conversion contact fields before creating observations.

Unresolved click sessions expire after at most 90 days (or shorter configured
event retention). Privacy erasure covers linked and unresolved aliases, verified
key provenance, lead rows, queued lead IDs and capture/attribution events.

The SDK source changes are compiled by the existing server/container frontend
build. Do not manually edit generated `dist/sdk` files or bypass its manifest
hash/size checks.

## Owned audiences and campaign results

Enter an ad account ID and separate advertising/system-user access token in the
source connection. Marketing access generally requires `ads_management` for
custom audience creation/replacement and `ads_read` (or equivalent granted
management access) for insights. Complete Meta's customer-list terms and business
permissions before attempting writes.

Audience → **Meta results** → create audience. Kinship creates a customer-list
custom audience and linked connected Meta destination. Only these owned mappings
are eligible for connected writes and reporting; arbitrary remote audience IDs
cannot be substituted. One owned mapping per source/segment is reused on retries.
If remote creation times out before its ID is returned, inspect Ads Manager for
an unused empty audience before retrying; creation is not blindly retried.

Use the ordinary activation wizard to send that segment to the created
destination. Its shared eligibility pipeline applies `ads_personalization`
consent, suppressions, required identifiers and household deduplication. Uploads
use normalized SHA-256 identity-only records and full-replacement sessions.
Any remote rejection, including an unsupported empty replacement, fails the run
explicitly; it never pretends members were removed. Check Meta's receipt before
rerunning a partially delivered replacement. Existing manual-upload Meta CSV
destinations remain available.

**Refresh results** loads the last 30 completed UTC days. Daily scheduler jobs
refresh owned-audience reporting. Results find ad sets explicitly including
the owned custom audience, then retrieve daily spend, reach and conversion
actions. The UI shows currency, refresh time, job errors and run state.

Meta reports at the **ad-set level**, not exact audience-level spend/reach when
audiences overlap, lookalikes/expansion are used, or other targeting is included.
Rows are labelled accordingly and must not be added across audiences. Conversion
count selects one reported giving/signup/lead action, not overlapping aliases;
the action is identified in each row's attribution note. This is aggregate
reporting, not person-level Meta impression or viewing ingestion.

## Video-privacy guard

The restriction applies to **all** Meta destinations, including manual CSVs,
connected audiences and runs queued before deployment:

* Rules permit only approved identity/giving fields, consent and explicitly
  allowlisted non-viewing events. VOD/viewing, plan/activity proxies, enrichment,
  resource filters and unknown fields are denied.
* Nested audience references are checked recursively. Editing a child cannot
  introduce viewing rules into an existing Meta audience.
* Export fields are identity-only. Constants, aliases for viewing fields and
  generic all-attributes exports cannot bypass the policy.
* Graph `/events` calls have an independent Conversions API payload allowlist:
  giving/registration/lead/contact events, hashed identity/click fields and
  monetary value/currency only. No content IDs, titles, URLs or arbitrary custom
  data. This adds a guard, not a new automatic CAPI forwarding service.
* Generic webhooks to Meta/Facebook/Instagram domains are blocked. Use the
  guarded adapter, not an arbitrary webhook relay.

## Server verification

No runtime verification was performed in Replit. Before enabling a real Page,
use a disposable database and Meta test lead/test-user facilities on the server:

* Run `tests/test_meta_capture.py` and `tests/test_meta_capture_pg.py` plus the
  existing identity, consent, erasure, activation and SDK suites.
* Exercise token/app/Page mismatches, missing permissions, correct/incorrect
  webhook verification/HMAC, duplicate deliveries, expired tokens and 429 retry.
* Backfill then replay leads; verify stable Master IDs, form/source evidence,
  timestamp/wording, opt-out precedence and no permission inferred for other forms.
* Page → signup/donation identify: verify attribution appears once, identifiers
  and profile count are unaffected by shared click cookies, malformed properties
  do not create contacts, and suppression blocks new observations.
* Valid/expired/wrong-app Facebook Login; Page contact quick reply versus PSID
  alone; conflicting system-key protection and unmerge evidence.
* Block direct/nested viewing rules, viewing mappings, legacy queued exports,
  unsafe CAPI payloads and generic webhook bypasses before any network write.
* Create a Meta test audience, run a consent-filtered replacement and inspect
  receipts; refresh ad-set insights and reconcile currency/action definitions.
* Erase a linked partner and query **every table** for their profile ID/keys,
  including anonymous sessions, pending contacts, lead/job provenance and events.
