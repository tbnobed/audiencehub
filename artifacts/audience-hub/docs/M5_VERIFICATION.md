# M5 verification

## Delivered

Partner audience list, tree builder and detail pages are connected to the segment
API. All DSL node families, typed operators, pinned windows, validation paths,
role-sensitive samples/JSON, dependency-aware materialization, scheduled refresh,
history, audit-logged CSV export and six editable templates are implemented.
Activation-time materialization is exposed as a helper for the later activation
milestone; activation execution itself is not implemented by M5.

## Evidence

- Final backend suite before the history-export addition: **385 passed, 1 skipped**.
- The subsequent history-export addition passed its focused PostgreSQL test,
  including viewer denial, exact CSV output and persisted audit entry.
- Frontend: **9 logic tests, 12 render/interaction tests** passed; existing profile
  tests remained **125/125**. Typecheck and production build passed.
- Signed-in browser lifecycle passed at 1280×900 and 1024×768: nested rule editing,
  duplicate/reorder/delete, live preview and per-rule impact, draft persistence,
  invalid JSON node-path feedback, active save, queued refresh completion, count
  history, table/CSV export and audit, duplication and archive confirmation.
- Unsaved edits survived the 10-second detail polling interval.
- The browser test archived only its own two audiences. All six seeded templates
  remained unchanged. Role restrictions also have backend and frontend coverage;
  the browser lifecycle was performed as admin, not as a separate viewer journey.

The browser's selected definition matched zero partners; preview, sample, members
and materialized count consistently agreed. Its 6.2 ms preview is a smoke check,
**not a medium-scale performance result**. Medium-scale preview timing remains
unmeasured; no 500k sandbox benchmark was run.

## Operational notes

See `SEGMENTS.md` for scheduling, trait-success barriers, reference/cycle rules,
delta pruning and controlled migration instructions. M4's separate five-pass
pre-M5 gate and production benchmark command are in `M4_VERIFICATION.md`.
Disposable benchmark/test clusters and generated fixtures were removed; portable
verification reports remain in this documentation directory.