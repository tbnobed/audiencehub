---
name: Audience Hub seed seasonality
description: Why a weighted year-end gift generator produces only a modest aggregate November–December giving increase.
---

In seed acceptance reports, show the November–December share for all gifts and, when explaining the synthetic seasonal weighting, the share for nonrecurring gifts separately. Do not describe the aggregate as a large year-end spike without checking it.

**Why:** The seed weights randomly dated gifts toward November and December, but monthly recurring gifts dominate the gift volume and are spread evenly through the year. In the 50,000-person acceptance run, nonrecurring giving had a clear year-end concentration while the all-gift share was only modestly above a uniform two-month share.

**How to apply:** When changing seed generation or reporting annual giving sanity figures, query both populations after import. If a strong aggregate spike is a requirement, change the seed mix deliberately and rerun the clean acceptance load rather than presenting nonrecurring seasonality as the aggregate result.

Seed history must be relative to one UTC `as_of`, defaulting to today, never to
fixed demo years or the maximum observed gift date. Explicit dates are for
reproducibility. The earlier 35–45% active / 10–15% lapsing demo target is
superseded by the real-account proportions in [Zeta replacement scope](zeta-parity-scope.md).
Keep imported model-score recency separate from computed donation recency.

**Why:** The user found that fixed historical seeds made current dashboards,
recency templates and RFM misleading despite otherwise plausible distributions.

**How to apply:** Verify the imported small seed with the same reference for
traits, rollups and templates. Five rolling calendar years usually span six
year labels; report partial boundary years separately instead of fabricating
future gifts to satisfy full-year seasonality or growth targets.

Gift-frequency/amount realism must not change the accepted audience recipient
sets. Increase repeat donations within the existing recent cohort, not by
spreading recent gifts to additional people. Preserve Basic One-Time membership
when deriving imported frequency from the ledger.

**Why:** The user explicitly required every audience membership, identity key
proportion and as_of anchoring to stay fixed while increasing gift volume.

**How to apply:** Non-lapsed imported RFM can include 13–24-month lapsing partners;
do not treat all nonzero imported recency scores as active-in-12-month donors,
which would silently expand the TBN+ 720-day donor audience. Report actual
seasonal dollar shares separately from intended date weights.