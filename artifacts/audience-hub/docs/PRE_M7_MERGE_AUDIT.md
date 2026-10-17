# Earlier-item merge audit

The following commits were checked individually with
`git merge-base --is-ancestor <commit> HEAD`. Each returned success against
`6ce178e68b9315e27f727fc62fb1f0aa12e2b172`.
The locally recorded `origin/main` also pointed at that HEAD; no fresh remote
fetch or production verification is implied.

| Requested item | Included commit | Evidence |
| --- | --- | --- |
| Dashboard rollups and per-card loading | `777540ef350ce8962bfd22743e8381b7bce868fd` | `backend/app/dashboards/rollups.py`; five independent card queries and states in `src/components/dashboard/overview.tsx`; dashboard frontend and PostgreSQL tests |
| Data Health badge counts open issues | `777540ef350ce8962bfd22743e8381b7bce868fd` | Sidebar uses `shell.open_issues`; health tests distinguish open failures/review items from unresolved-record backlog and verify dismissal |
| Font data-URI fix | `777540ef350ce8962bfd22743e8381b7bce868fd` | Local WOFF2 faces, Vite `assetsInlineLimit: 0`; built-CSS test rejects data/remote font URLs without weakening CSP |
| Profile hardening and 50-profile render test | `2dc92a8ee8f56dacef3ca83fa08b02a0b4e37984` | Actual mounted React page and tabs tested against captures for profile IDs 1–50 as admin and viewer; section-boundary coverage |
| `kinship load-status` | `777540ef350ce8962bfd22743e8381b7bce868fd` | Console command, scoped import/dependent-work tracking, wait/failure/timeout tests |
| M5 implementation | `db69fb03aefdb5308526eff39d2edae0626f893d` | Segment engine, APIs, materialization, templates and builder |

## Profile crash qualification

The profile hardening and render coverage are merged. A production crash
**root-cause fix is not established**: the original component also passed the
same captured responses. See `PROFILE_RENDER_REGRESSION.md`.
The render suite and error containment must not be described as proof that an
unreproduced production exception has been corrected. A failing reproduction or
privacy-safe production exception evidence is needed to close that claim.

These are inclusion checks, not fresh performance or production acceptance
results. Historical test evidence is linked above and in the relevant reports.
