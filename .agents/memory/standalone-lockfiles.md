---
name: Standalone container lockfiles
description: Why a workspace-generated npm lockfile can fail in a standalone Docker image
---

For independently packaged artifacts inside this pnpm workspace, create the npm lockfile in an isolated directory using the same Node/npm version as the Docker build stage.

Match exact Node and npm versions, not just their major versions, when validating
container installation failures.

**Why:** A successful standalone npm 10.9.9 check did not reproduce a user's
container install that crashed under npm 10.8.2. A cached moving Node image tag
can retain a different bundled npm version.

**How to apply:** Verify version output and use a fresh package cache for the
standalone check. Do not describe it as a full Docker build or as proof that
the host's installation error has been reproduced.

**Why:** An npm-generated lockfile created inside the parent pnpm workspace contained local link metadata. It looked valid but npm 10 inside a standalone Node 20 image rejected it with a misleading "existing package-lock.json" error. Generating the lock in isolation made `npm ci` and the image build pass.

**How to apply:** If a self-hosted artifact has its own package.json and Dockerfile, test a real image build when Docker is available; do not assume a workspace-generated npm lockfile is portable.

Standalone npm lockfiles must use publicly resolvable package URLs, not Replit-internal registry URLs.

**Why:** Even a lockfile generated outside the workspace can retain an internal registry tarball URL. External Docker hosts cannot resolve that hostname, so `npm ci` fails with `ENOTFOUND`.

**How to apply:** After dependency changes, check all resolved URLs in the standalone lockfile for internal hosts. Use the corresponding public npm tarball URLs without changing versions or integrity hashes; verify the public tarball against the recorded integrity. This is a portability check, not a workaround for package security blocks.