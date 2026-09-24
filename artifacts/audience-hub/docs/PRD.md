# Audience Hub: product requirements (MVP)

## Purpose

Replace the commercial Zeta Global CDP ($650k/yr) with a self-hosted platform that OBTV/TBN owns. The MVP has to let the marketing and development teams:

1. Unify donor, viewer, app, email, and contact-center data into one profile per person.
2. See dashboards on giving, donor retention, and engagement.
3. Build audience segments without writing SQL.
4. Push those segments to email, ad platforms (hashed audiences), and other systems.
5. Import purchased third-party enrichment data (from Zeta on a data-only contract, or from other compilers), track its license expiry, and use it in segments.

## Users and roles

| Role | Can do |
|---|---|
| admin | Everything: users, sources, API keys, destinations, deletion requests, source priority, trait catalog |
| analyst | Import data, view full profiles (unmasked PII), build segments, run activations and exports |
| viewer | View dashboards, segment counts, and profiles with email and phone masked; no exports |

Roles come from Authentik group claims in production (see `SECURITY_PRIVACY.md`).

## In scope for the MVP

- **Sources:** CSV sources (donor CRM, giving platform, ESP export, Five9 or contact-center export, enrichment vendor files) and event API sources (web and app tracking).
- **CSV import wizard:** upload → column mapping with auto-suggest → validation preview → run → progress and error report. Record types: `contact`, `gift`, `event`, `enrichment`, `consent`.
- **Event ingestion API:** `/v1/track`, `/v1/identify`, `/v1/batch` with per-source write keys, plus a small first-party JavaScript SDK served at `/sdk/ah.js`.
- **Deterministic identity resolution:** email, phone, and source external IDs, with transitive merging, a blocklist, and survivorship by source priority. See `IDENTITY_RESOLUTION.md`.
- **Profiles:** search, list, and a detail page (identifiers, source records, consent, computed traits, enrichment, gifts, event timeline, merge history).
- **Computed traits:** giving (RFM, lifetime and 12-month totals, donor status) and engagement traits, recomputed nightly and after imports. See `DATA_MODEL.md`.
- **Dashboards:** overview KPIs, giving trends, retention, source overlap, and data health.
- **Segments:** a JSON rule DSL, a visual builder, live counts and sample profiles, materialized membership, and count history. See `SEGMENTS.md`.
- **Activation:** CSV export, hashed audience files (Google Customer Match and Meta presets), and signed webhooks, run on demand or on a schedule with run history. See `ACTIVATION.md`.
- **Consent and suppression:** per-channel consent, suppression on deletion, and consent-aware activations.
- **Deletion requests:** hard delete by email or phone, with a suppression hash so records aren't recreated on re-import.
- **Audit log.**

## Out of scope (later phases)

- ClickHouse for high-volume events (the design keeps the events table isolated so it can move).
- Probabilistic or fuzzy matching (Splink), householding, and unmerging via the UI.
- Real-time journeys and triggered multi-step flows.
- Native connectors that pull from vendor APIs (ESP, CRM, Five9). The MVP uses CSV and the event API.
- ML models (propensity, lookalikes).
- Direct API pushes to Meta and Google. The MVP produces upload-ready files; API sync comes later.

## Scale targets

- 2 million profiles, 5 million gifts, 20 million events.
- A 1-million-row CSV import completes in under 15 minutes on an 8 vCPU / 32 GB host.
- Typical segment count preview in under 5 seconds (10-second statement timeout).
- A full nightly computed-trait recompute in under 20 minutes.

## Success criteria

- Dashboards match the numbers the team gets from Zeta for the same first-party data, within an explained tolerance.
- Analysts can rebuild their 10 most-used Zeta segments in the builder without engineering help.
- Hashed audience files upload to Google Ads and Meta with expected match rates.
- No PII leaves the server except through an explicit, audited activation or export.

## Migration notes (from Zeta)

- Before contract end, export all profiles, events, segment definitions, and appended attributes from Zeta.
- Import Zeta profile exports as a `contact` source. Import Zeta-appended attributes as an `enrichment` source with vendor `zeta` and `license_expires_at` set to the date the contract says the retention right ends.
- Recreate segment definitions from the Zeta export in the builder and compare counts side by side.

## Glossary

- **Profile:** one resolved person. Many source records can belong to one profile.
- **Source record:** one row as it arrived from one source, kept verbatim for lineage.
- **Identifier:** a normalized email, phone, or `source:external_id` used for matching.
- **Trait:** a computed attribute on a profile (for example `ltv_total`, `donor_status`).
- **Enrichment attribute:** a vendor-supplied attribute with a license expiry.
- **Segment:** a saved rule set that selects profiles.
- **Destination:** where a segment is sent (CSV, hashed audience file, webhook).
