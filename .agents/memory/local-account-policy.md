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

Keep the observed session query stable when changing authentication state.
Treat an anonymous session probe as signed-out data, not a global cache-expiration
event; clear private data without removing that observed query.

**Why:** evicting the session query from inside its own unauthorized-response
handler can recreate the query repeatedly and keep the login screen loading.

**How to apply:** keep login/logout/session-expiration transitions separate from
generic API failure handling and bound the initial session request.

Anonymous authentication checks must not invalidate the CSRF session of an open
login form. Remove authentication privileges separately from the anonymous token.

**Why:** read-only session checks can run while a login form is open; clearing the
whole cookie makes its legitimate CSRF header mismatch before password checking.

**How to apply:** preserve anonymous CSRF during unauthorized reads, avoid polling
when signed out, and refresh the token/cookie pair before submitting credentials.
Do not fix these failures by weakening CSRF validation.

Suppression peppers cannot simply be replaced and discarded. Deleted identifiers
may have no remaining cleartext from which to regenerate permanent tombstones.

**Why:** losing an old pepper can silently allow previously suppressed people to
be re-imported or exported; encryption-key rotation is a different operation.

**How to apply:** retain old suppression peppers as read keys for as long as their
tombstones must remain effective, and review every matching/export path when
changing hashing. Never save key values in memory, docs, CLI arguments or logs.
