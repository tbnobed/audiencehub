---
name: Development key stability
description: Keep encrypted destination data and deletion tombstones valid across restarts
---

Development encryption and suppression keys must survive workflow restarts; do not restore per-start random key fallbacks.

**Why:** M7 persists encrypted destinations/deletion requests and HMAC suppression tombstones. Regenerating keys on restart makes ciphertext unreadable and previously erased identities unrecognizable.

**How to apply:** Preserve explicit configured keys. The development launcher may derive missing keys with separate domains from the existing stable workspace session secret; production keeps its explicit key configuration. Never log or persist key values in agent memory.
