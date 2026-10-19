---
name: Audience lifecycle
description: Bootstrap versus ongoing audience pause behavior
---

Create the audited templates active and daily on seed/reset, but do not silently
reactivate existing paused audiences on ordinary API startup.

**Why:** Bootstrap must produce usable partner counts; repeated application
restarts must not undo an operator's deliberate pause. Explicit template
activation and a new seed load are opt-in ways to reactivate the catalog.

**How to apply:** Preserve existing lifecycle choices in startup template
maintenance. Pause retains counts and memberships and uses the existing draft
state rather than introducing a second inactive state.
