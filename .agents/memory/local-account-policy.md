---
name: Local account boundary
description: User-directed authentication scope and irreversible suppression-key constraints.
---

Use local accounts only for now; do not enable SSO as part of routine maintenance.
Keep credentials separate from user identity and ownership so a later OIDC adapter
does not require recreating users.

**Why:** the user explicitly requested local accounts and no SSO for M8, while
requiring a future OIDC option without reworking users.

**How to apply:** preserve this boundary when changing authentication or deployment
instructions. A future SSO change needs a new user request.

Suppression peppers cannot simply be replaced and discarded. Deleted identifiers
may have no remaining cleartext from which to regenerate permanent tombstones.

**Why:** losing an old pepper can silently allow previously suppressed people to
be re-imported or exported; encryption-key rotation is a different operation.

**How to apply:** retain old suppression peppers as read keys for as long as their
tombstones must remain effective, and review every matching/export path when
changing hashing. Never save key values in memory, docs, CLI arguments or logs.
