---
name: Benchmark files and development watchers
description: Keep generated PostgreSQL data outside frontend and Python reload scans.
---
Keep frontend CSS source discovery, frontend watchers and Python reload scans scoped to application source rather than the whole artifact.

**Why:** A large isolated benchmark created gigabytes of PostgreSQL files under the backend cache. Broad scans consumed CPU and memory and made the preview stop responding despite healthy startup logs.

**How to apply:** When adding generators, benchmark directories or build tooling, exclude generated databases, uploads and caches from watching, CSS discovery and publishing. Do not treat a running dev-server process as proof that it responds.
