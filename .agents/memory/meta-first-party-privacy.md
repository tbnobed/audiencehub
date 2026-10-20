---
name: Meta first-party privacy boundaries
description: The user’s Meta identity provenance and video-privacy restrictions.
---

Meta capture extends the existing system with a Facebook Login-only app-user
system key and a Page-scoped PSID that links only after the person provides
email or phone. Click IDs and cookies are attribution, never person keys.

**Why:** The user explicitly required these provenance boundaries while adding
Meta first-party capture, rather than a replacement identity resolver.

**How to apply:** Do not trust raw CSV/SDK attributes as proof of Facebook Login
or a Page contact exchange. Preserve every original key and Master ID rule.

No viewing or VOD attributes may be included in any Meta destination,
Conversions API payload or audience rule. Only giving, signup and consent-safe
engagement data may be used.

**Why:** The user explicitly required avoiding video-privacy exposure.

**How to apply:** Enforce this server-side for manual files and connected
delivery, nested audience references, queued runs and future CAPI callers.
Do not rely on UI filtering or allow generic webhooks to bypass the restriction.
Report Meta ad-set insights honestly; targeting overlap/expansion does not
provide exact audience-level attribution.
