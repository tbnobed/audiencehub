---
name: Verification scope and product copy
description: User constraints for benchmark location and partner terminology
---

Stop 500k performance measurements in this sandbox. The user will run remaining timings on their production server (16 cores); the sandbox has 4 vCPU and storage-quota limits.

**Why:** The user explicitly moved final timing to their server after unreliable sandbox measurements and quota failures.

**How to apply:** Keep local correctness tests isolated. The specifically authorized exception is comparing resolver versions on a 250k contact fixture, three runs each with medians. Production-container benchmarks must use an isolated schema and never touch live data.

Use “partners” in all user-facing copy.

**Why:** The user explicitly requested this terminology when starting Milestone 5.

**How to apply:** Use partner terminology in segment names, summaries, labels, errors and templates; retain existing internal/API field identifiers where compatibility requires them.