# Decisions

- Milestone 1 only creates foundation tables. No donor, viewer, or app data is imported into Replit. The `seed` CLI exits explicitly until Milestone 2 provides the synthetic CSV generator.
- The frontend is in the artifact root (`src/`) in this development workspace so the preview runner can serve it. The self-hosted Docker build uses this directory and does not depend on the workspace's shared Express server or packages.
- In development the launcher generates process-local throwaway secrets when none were provided. Production always reads real values from environment variables and refuses dev authentication.
- The scheduler enqueues a harmless hourly no-op in M1 to verify unique scheduled windows before data-maintenance tasks arrive in later milestones.