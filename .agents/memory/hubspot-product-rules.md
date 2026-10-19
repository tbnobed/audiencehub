---
name: HubSpot product rules
description: TBN's email platform, identity coverage, and cross-export unsubscribe requirement.
---

HubSpot is TBN's email platform. Every profile carries `hubspot_record_id`.
An unsubscribe in HubSpot must suppress that email in every Kinship email export,
not only the HubSpot destination.

**Why:** The user explicitly stated these facts and required cross-destination
unsubscribe enforcement when requesting the integration.

**How to apply:** Keep HubSpot contact IDs as the existing real identity key.
Do not isolate its opt-outs to one destination or permit CSV/export settings to
bypass the email suppression.
