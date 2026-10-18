---
name: SQL JSON literal bindings
description: Avoid accidental SQLAlchemy bind parameters in raw JSON literals.
---
Use bound JSON parameters or PostgreSQL `jsonb_build_object` rather than compact JSON literals inside SQLAlchemy text/Alembic execute strings.

**Why:** JSON fragments such as `:true`, `:false`, and `:123` were interpreted as bind parameters, breaking both PostgreSQL test fixtures and a migration before benchmarks could start.

**How to apply:** Check new migration and fixture SQL containing JSON, and execute migrations against disposable PostgreSQL rather than relying on Python syntax checks.
