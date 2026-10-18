---
name: Source evidence envelopes
description: Why people, model scores and identity lookup imports must retain separate provenance
---

People, model-score and identity-lookup exports are separate evidence even when
they share the same Zeta profile key. Shared keys link that evidence; they do not
authorize one feed to replace another feed's raw record.

**Why:** Zeta exit exports reuse a person's CDP/UID across different feeds.
Treating that reuse as a replacement snapshot can discard the person's name,
contact fields or lookup provenance when a later model/lookup file arrives.

**How to apply:** Keep distinct feed envelopes when changing import/upsert or
retry behavior. Preserve the real identity keys and the accepted resolver;
do not solve provenance collisions by changing who the identity graph joins.
