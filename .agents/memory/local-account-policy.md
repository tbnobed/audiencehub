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

Account authentication and email delivery must work on the user's own server
without Replit runtime dependencies. Support any standard TOTP authenticator
app, not one particular vendor. SendGrid is the account email relay.

**Why:** the user explicitly requested “no replit depedancies” and that users
“should be a allowed to use any authenticator app.”

**How to apply:** use standard authenticator enrollment and direct provider
connections with locally managed credentials; do not substitute managed Replit
authentication or connector credentials for this self-hosted account system.

Show TOTP in its own sign-in popup only for users who have it enabled, after
their password is accepted. Do not place an optional code field on every user's
initial password form or grant authentication before the second factor succeeds.

**Why:** the user explicitly requested “TOTP should be on it's own popup if it's
enabled for the user.”

**How to apply:** preserve the separate password and second-factor steps in the
browser while retaining authenticator-app and recovery-code support.

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

Do not let background session refreshes unmount enrollment before the user can
save the one-time recovery codes.

**Why:** enabling required MFA changes the router's gate condition; refreshing
session state immediately can remove the only displayed recovery-code copy.

**How to apply:** keep the enrollment completion transition under the user's
control while adopting any rotated CSRF token immediately.

Suppression peppers cannot simply be replaced and discarded. Deleted identifiers
may have no remaining cleartext from which to regenerate permanent tombstones.

**Why:** losing an old pepper can silently allow previously suppressed people to
be re-imported or exported; encryption-key rotation is a different operation.

**How to apply:** retain old suppression peppers as read keys for as long as their
tombstones must remain effective, and review every matching/export path when
changing hashing. Never save key values in memory, docs, CLI arguments or logs.
