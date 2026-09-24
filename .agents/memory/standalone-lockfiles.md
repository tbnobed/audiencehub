---
name: Standalone container lockfiles
description: Why a workspace-generated npm lockfile can fail in a standalone Docker image
---

For independently packaged artifacts inside this pnpm workspace, create the npm lockfile in an isolated directory using the same Node/npm version as the Docker build stage.

**Why:** An npm-generated lockfile created inside the parent pnpm workspace contained local link metadata. It looked valid but npm 10 inside a standalone Node 20 image rejected it with a misleading "existing package-lock.json" error. Generating the lock in isolation made `npm ci` and the image build pass.

**How to apply:** If a self-hosted artifact has its own package.json and Dockerfile, test a real image build; do not assume a workspace-generated npm lockfile is portable.