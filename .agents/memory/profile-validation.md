---
name: Profile render validation
description: Evidence required before claiming a production profile crash is fixed
---

Compare the original and revised component against the same real seed-generated
API responses in mounted ReactDOM tests, not only hand-written fixtures or SSR.

**Why:** Production profile crashes persisted despite successful API checks and
fixture renders. Subsequent full-medium captures also passed on the original
component; that establishes coverage, not a demonstrated root-cause fix.

**How to apply:** Keep crash reproduction, null-safety improvements, and section
containment as separate claims. If original and revised versions both pass,
use privacy-safe production exception evidence rather than inventing a cause.