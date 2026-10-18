---
name: Segment performance constraints
description: Canonical property precedence, sample cost and production-only evidence
---

Preserve per-field source precedence when optimizing segments; filtering raw
source evidence before selecting its canonical winner changes audience counts.
Do not replace imported RFM/account evidence with computed Kinship traits.

**Why:** The user accepted all 23 audience counts on the production 500k dataset;
this work targets latency, not audience definitions or identity behavior.

**How to apply:** Keep count and limited sample costs distinct from optional
full-membership breakdowns. Preserve historical activity OR semantics when one
activity timestamp is future-dated. Report measured population with latency;
500k results do not establish 8M performance. Runtime verification remains on the
user's server, never via tests/loads/benchmarks in Replit.
