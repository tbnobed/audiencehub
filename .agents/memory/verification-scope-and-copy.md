---
name: Verification scope and product copy
description: User constraints for benchmark location and partner terminology
---

Stop 500k performance measurements in this sandbox. The user will run remaining timings on their production server (16 cores); the sandbox has 4 vCPU and storage-quota limits.

**Why:** The user explicitly moved final timing to their server after unreliable sandbox measurements and quota failures.

**How to apply:** Keep local correctness tests isolated. The user now explicitly requires 250k-person acceptance with all five sources, gifts, resolution, attachment, traits and rollups. A contact-only pass is not full-load evidence. Report per-phase timings and a qualified 500k/16-core projection; the production target is under 60 minutes. Production-container benchmarks must use an isolated schema and never touch live data.

Use “partners” in all user-facing copy.

**Why:** The user explicitly requested this terminology when starting Milestone 5.

**How to apply:** Use partner terminology in segment names, summaries, labels, errors and templates; retain existing internal/API field identifiers where compatibility requires them.

Do not change the import, identity-resolution or attachment pipeline until the user sends the production timings.

**Why:** The user is measuring the 500k production load and explicitly froze those pipelines while requesting worker lifecycle fixes, M7 follow-ups, UI completion, then M6.

**How to apply:** Keep shutdown/session management changes outside pipeline algorithms. Build M6 ingestion around existing resolution and attachment interfaces rather than modifying the frozen pipeline.