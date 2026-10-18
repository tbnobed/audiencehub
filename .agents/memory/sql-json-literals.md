---
name: SQL JSON literal bindings
description: Avoid accidental SQLAlchemy bind parameters in raw JSON literals.
---
Use bound JSON parameters or PostgreSQL `jsonb_build_object` rather than compact JSON literals inside SQLAlchemy text/Alembic execute strings.

**Why:** JSON fragments such as `:true`, `:false`, and `:123` were interpreted as bind parameters, breaking both PostgreSQL test fixtures and a migration before benchmarks could start.

**How to apply:** Check new migration and fixture SQL containing JSON, and execute migrations against disposable PostgreSQL rather than relying on Python syntax checks.

The same parser hazard applies to noncapturing regex groups in SQL literals:
`(?:_return)` introduces a spurious `_return` bind. Prefer equivalent patterns
without colon syntax, preserving PostgreSQL substring's first-capture semantics.

**Why:** A production repair dry run failed before reaching PostgreSQL because
of regex binding, and its generic SQLSTATE-only error concealed the cause.
Report construction errors separately without dumping SQL parameters.

**How to apply:** Review raw SQL regexes as well as JSON. Respect the user's
code-only rule: verification for this project runs on their server, not Replit.
