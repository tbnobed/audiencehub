---
name: Audience Hub writable paths
description: Development and production have different writable filesystem roots for import files.
---

Keep Audience Hub's development upload/export paths inside the workspace, while production uses its mounted persistent data volume.

**Why:** The Replit development container rejected writes to `/data` with a read-only filesystem error, even though the self-hosted Docker Compose stack explicitly mounts a writable volume there. A working production default is not a usable development default.

**How to apply:** When adding file-based flows or changing defaults, use the local dev launcher to set writable workspace paths and keep production paths on the Compose volume. Do not assume the development host can write to `/data`.