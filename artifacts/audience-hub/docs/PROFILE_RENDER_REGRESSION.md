# Profile rendering regression

## Verified coverage

The committed capture was generated from the complete medium seed:
50,000 input profiles, density `medium`, random seed `20250308`.
All five files passed through the real importer, resolver, trait recomputation,
and authenticated FastAPI endpoints in disposable PostgreSQL.

The load accepted 680,263 rows and intentionally rejected five. After resolution,
it contained 49,143 profiles, 536,865 gifts, 17,765 events and 196,056 enrichment
values. No application or production database was used.

The React Testing Library suite mounts the actual page, query hook, router and
Radix tabs with captured API transport responses. It covers IDs 1–50 as admin
and viewer, plus prospects, lapsed profiles, missing contact fields, missing
traits/consents/enrichment, expired enrichment, merged profiles, events without
gifts, and Five9-only profiles. Edge-case captures come from real API responses
inside rolled-back database transactions. Merged profiles currently show the
existing not-found/retry response to the API's HTTP 301; canonical navigation is
not implemented.

Run `npm run test:profiles` in the artifact directory. CI runs the committed
capture, regenerates it with the full medium seed, and runs the suite again.
Capture provenance and hashes are in `scripts/fixtures/profiles-medium.json`.

## Results and limitation

125 RTL tests pass, including actual section-boundary throw/retry behavior and
development/production diagnostics.

**The original page also passed the identical captured API cases. The reported
production exception has not been reproduced and its root cause is not known.**
Null-safe rendering and section containment are defensive improvements, not
proof of a fix for that unidentified exception.

## Production diagnostics

Migration `0022_client_errors` enables authenticated, CSRF-protected reporting to
`POST /api/client-errors`. The System page exposes reports to administrators.
The server and client retain only allowlisted error categories, components,
routes and numeric profile IDs; raw messages and arbitrary stack text are
discarded to avoid storing personal data. Reports are rate-limited, capped at
1,000 rows, and expire after seven days.

Development boundaries show the original message and component stack.
Production boundaries show a small section-level fallback with Retry.
An actual production report is still needed to identify the remaining crash.