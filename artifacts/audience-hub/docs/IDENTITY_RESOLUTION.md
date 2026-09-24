# Identity resolution (deterministic, MVP)

## Goal

Every source record and every identified event belongs to exactly one active profile. Two records that share a strong identifier end up on the same profile, transitively. No fuzzy matching in the MVP.

## Normalization (`app/identity/normalize.py`)

| Identifier | Rule |
|---|---|
| Email | Trim, lowercase, validate with `email-validator` (no DNS check). Gmail/googlemail: remove dots and `+tag` from the local part, map `googlemail.com` to `gmail.com`. For other domains remove only `+tag`. Store the original in `source_records.attributes.email_raw` |
| Phone | `phonenumbers.parse(value, DEFAULT_PHONE_REGION)`, keep only if `is_valid_number`, format E.164. Extensions are dropped |
| External ID | `f"{source_key}:{external_id.strip()}"` |
| anonymous_id / user_id | Trimmed string, max 200 chars; `user_id` is namespaced as `source_key:user_id` |
| Names | Trim, collapse whitespace, title-case only if the input is all upper or all lower |
| Postal code | US: first 5 digits |

Invalid values are dropped from matching (the record still imports), and the import error report lists them as warnings, not rejections.

## Blocklist

Identifiers on `identifier_blocklist` are never used for matching. Seed with:
- Emails: `test@test.com`, `none@none.com`, `noemail@*`, `no@email.com`, `na@na.com`, anything at `test.com`, and shared office or role addresses that admins add later. Do not blocklist `example.com`/`example.org`: the synthetic seed data uses those domains.
- Phones: `+10000000000`, `+11111111111`, `+15555555555`, `+12345678900`, and any number whose 10 national digits are all the same.
- Automatic safety net: any email or phone that would link more than 25 distinct external IDs from a single source gets auto-blocklisted with reason `high_cardinality` and appears on the Data Health page for review.

## Matching keys, in priority order

1. `external` (same source + same external ID) — always the same person.
2. `email`
3. `phone`
4. `user_id` (from events)
5. `anonymous_id` (from events, only linked when an `identify` call carries a stronger identifier)

## Algorithm (`app/identity/resolver.py`)

Batch-oriented, runs inside `identity.resolve_batch` over up to 10k pending source records at a time.

1. Load pending records and extract their normalized identifiers (excluding blocklisted).
2. Look up all those identifiers in `identifiers` in one query → map identifier → profile_id.
3. Build a union-find over: each pending record, each existing profile it touches, and records in the batch that share identifiers with each other.
4. For each connected component:
   - If it touches no existing profile → create one profile.
   - If it touches exactly one → attach to it.
   - If it touches several → merge. Winner = oldest profile (lowest `first_seen_at`, then lowest id). For each loser: re-point `identifiers`, `source_records`, `gifts`, `events` (in batches), `consents` (apply consent precedence), `enrichment_values` (keep the most recent import per key), `segment_membership` (drop; next materialize fixes it); set `merged_into_id`; insert `profile_merges` with the linking identifier as reason.
5. Insert any new identifiers for the component's profile (`ON CONFLICT DO NOTHING`, then re-check for conflicts created by concurrent jobs and loop once).
6. Recompute survivor fields for affected profiles (see below) and mark `source_records.resolved_at`.
7. Mark affected profiles dirty for `traits.recompute`.

Concurrency: take `pg_advisory_xact_lock(hashtext(identifier))` for every identifier in a component before merging, sorted to avoid deadlocks. Only one `identity.resolve_batch` runs at a time by default (dedupe key `identity`), which is enough for MVP volumes.

Events: when an `identify` event has `user_id` or email/phone traits, it creates or updates a source record for the event source and links its `anonymous_id` identifier to the resolved profile. Then `UPDATE events SET profile_id = $p WHERE anonymous_id = $a AND profile_id IS NULL`.

## Survivorship (`app/identity/survivorship.py`)

For each profile field (email, phone, name, address block):
1. Candidate values come from the profile's source records.
2. Pick the value from the source with the lowest `priority` number.
3. Tie → most recently updated source record.
4. Address fields travel as one block (don't mix city from one source with street from another).
5. Empty values never win over non-empty ones.

## Required tests

- Email normalization cases: case, whitespace, gmail dots, `+tags`, googlemail, invalid addresses.
- Phone: US formats `(214) 748-3647`, `214.748.3647`, `+1 214 748 3647`, `2147483647 x12` (extension dropped), invalid numbers (`123`, `(000) 000-0000`), international `+44 20 7946 0958`.
- Two records, same email, different sources → one profile.
- Transitive: A(email1), B(email1, phone1), C(phone1) arriving in three separate batches → one profile with two merges recorded.
- Merge moves gifts, events, identifiers, consents; loser has `merged_into_id`; `active_profiles` excludes it.
- Consent precedence during merge: opt-out survives.
- Blocklisted email on 500 records → 500 separate profiles, no merges.
- High-cardinality auto-blocklist triggers at the threshold.
- Re-importing the same file changes nothing (idempotent, zero new profiles, zero merges).
- Survivorship: lower priority number wins; empty never wins; address block stays together.
- Anonymous events are back-filled onto the profile after `identify`.

## Known limitations (document in the UI help)

- Shared family emails or phones will merge household members. This is accepted for MVP; the high-cardinality guard catches the worst cases.
- No unmerge in the UI. Admins can see merge history; an unmerge tool is a later phase.

## Profile and Data Health API

The profile read API is available at:

- `GET /api/profiles?search=&source_id=&has_email=&has_phone=&donor_status=&page=&page_size=`. Search uses PostgreSQL `pg_trgm` similarity over the maintained `profiles.search_text`; results contain only active profiles and return `{items,total,page,page_size}`. Source filtering uses linked source records. `has_email` and `has_phone` filter the profile's resolved contact fields; missing traits report donor status `prospect`.
- `GET /api/profiles/{id}` returns the profile's scalar fields and traits, plus `gifts`, `events`, `identifiers`, `source_records`, `merges`, `enrichment`, and `consents` arrays. Source-record history is ordered by source priority (lowest number first), then newest update. Expired enrichment values are omitted. A request for a merged profile returns HTTP 301 with its `merged_into` ID; deleted/unknown profiles return 404.
- `GET /api/data-health` returns `pending_count`, `merges_per_day`, `blocklist_hits`, `auto_blocklisted`, `rejected_rows`, and `recent_imports`. Pending means unresolved source records; blocklist hits are source records whose email/phone currently matches the blocklist.
- Admins may `POST /api/data-health/blocklist/{id}/approve` to record review while keeping the identifier blocked, or `POST /api/data-health/blocklist/{id}/unblock` to remove it from the blocklist. Both actions are audited. Only automatic `high_cardinality` blocklist entries are reviewable from this API.

All profile/list/detail/data-health responses mask email and phone values for the viewer role, including identifier values and nested source-record/event attributes. Analyst/admin callers receive unmasked values. Profile detail views write a `profile.view` audit entry for all roles.
