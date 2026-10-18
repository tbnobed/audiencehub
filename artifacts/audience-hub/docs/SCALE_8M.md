# Production scale: 8M profiles and 150M+ events

## Scope and evidence

This is a capacity and migration plan, not a claim that an 8M load has passed.
**No 8M generation, import or benchmark has been run.**

Two different workloads must not be treated as equivalent:

| Evidence | Population / facts | Observed time |
|---|---|---|
| User's production run, 16 cores | 491,088 profiles; 5,360,521 gifts; 388 unattached events | 4,058.6s tracked load; 80m29.931s shell wall time |
| New isolated audit seed, local | 50,000 profiles; 11,853 gifts; 76,581 events; 24,500 enrichment source rows | 231.26s test wall time |

The audit seed intentionally matches audience proportions. It does **not**
reproduce the production gift volume or a 132M-view historical backfill.
Scaling its total wall time by population alone would substantially understate
the event-heavy migration. Local build/test activity can also affect timings.

The local 500k audience run and its phase timings are recorded in
`AUDIENCE_ACCEPTANCE.md` when available. A failed or incomplete run must not be
used as a successful timing sample.

Current 500k status: **not accepted**. All five imports completed, but the
benchmark's in-process identity call timed out before audience measurements.
Consequently this plan uses the supplied production baseline, not an invented
successful local 500k full-pass duration. Another clean 500k rehearsal remains a
release gate after the harness timeout-context correction.

## Initial time budget

Using the observed production population as the reference:

- Population factor: `8,000,000 / 491,088 = 16.29`.
- Linear tracked-load extrapolation: `4,058.6 × 16.29 ≈ 18.4 hours`.
- Linear shell-wall extrapolation: `80.499 minutes × 16.29 ≈ 21.9 hours`.
- These are arithmetic baselines, **not delivery estimates**. They do not
  establish how much time was spent in identity versus transaction attachment.
- Plan an initial **24–48 hour migration window**, with durable checkpoints,
  until the 150M-event backfill and full identity pass are measured on the target
  hardware. This is an operational allowance, not a measured confidence interval.

For each measured phase `T500`, use the following worksheet:

| Phase | First approximation | Important nonlinearity |
|---|---|---|
| Contact import | `T500_contacts × 16` | JSON width, validation, WAL and index writes |
| Identity full pass | `T500_identity × 16` | Graph edges, component skew, sort spill and shared identifiers |
| Event import | `measured_event_seconds / measured_event_rows × 150M` | Index size, resource references, storage bandwidth |
| Gift/event attachment | Scale by each table's fact rows, not profile count | Join indexes, unmatched identifiers, dirty-page volume |
| Traits | Separate profile count from fact rows scanned | Full history versus incremental dirty-profile refresh |
| Rollups | Scale by fact rows and partition coverage | Grouping/sort spill, historical months, index-only scans |

Do not add benchmark count/materialization time to the ingestion estimate.
Report import, identity, attachment, traits and rollups separately on the actual
16-core server before committing to a migration deadline.

## Monthly event partitions

The current event table is not being repartitioned by this change. Introduce
range partitions on `occurred_at` in a separately rehearsed migration:

1. Create a new partitioned parent with monthly UTC boundaries, historical
   partitions, future partitions and a monitored default partition.
2. Preserve event idempotency. PostgreSQL partitioned uniqueness must include
   the partition key; retain an independent `(source_id, external_id)` receipt/
   identity registry if event uniqueness must span months.
3. Backfill one month at a time with row-count/checksum reconciliation and
   restartable checkpoints. Do not perform an unbounded rewrite while serving
   interactive traffic.
4. Rebuild foreign keys and profile deletion paths deliberately. Rehearse
   erasure across every partition and the deduplication registry.
5. Cut over with a documented write barrier and rollback plan. Retire the old
   table only after reconciliation and backup verification.

Partition pruning benefits bounded event windows. An unrestricted “ever viewed”
audience still spans all partitions; maintain a verified per-profile activity
summary where this becomes a repeated bottleneck.

## Parallel identity: correctness before concurrency

Do not divide a connected-components pass arbitrarily by source or profile ID:
an email/hash/phone edge can connect records across those boundaries.

- Keep one full set-based resolution after a bulk batch's contact, consent and
  enrichment inputs finish. Gifts and events remain outside the identity graph.
- Parallelize independent normalization/staging, key extraction and bounded
  fact import before attempting parallel graph mutation.
- For parallel component discovery, compute a global edge relation first;
  partition work only after connected components are identified, or use an
  iterative cross-partition boundary reconciliation algorithm.
- Give each component one owner. Acquire deterministic locks for merges and
  journal writes; preserve immutable Master IDs and conflicting-system-key
  rules under concurrent execution.
- Start with PostgreSQL query parallelism and measured `work_mem` limits.
  Sixteen CPU cores do not imply sixteen safe concurrent merge writers.
- Require identical membership, merge journals and ground-truth precision/
  recall against the serial pass before enabling any parallel resolver.

## Index strategy

- Retain indexed system/contact key lookup and the Master ID crosswalk indexes.
  Check key selectivity and conflicting-household fanout.
- Index attachment paths by source plus contact external ID, and normalized
  email/phone paths used for fallback. Avoid retry scans of unmatchable facts.
- On events, evaluate `(profile_id, name, occurred_at)` and
  `(name, occurred_at, profile_id)` against the actual audience workload.
  Use BRIN on chronological bulk data where selectivity makes it worthwhile.
- Keep resource-reference keys indexed; measure the join rather than adding
  blanket GIN indexes to every JSON payload.
- Promote frequently filtered canonical properties into maintained, typed
  projections only with source-priority semantics and freshness checks.
- Build large secondary indexes after historical staging where safe; use
  concurrent index creation for a serving database. Run ANALYZE before timing
  attachment/segment queries.
- Measure WAL, index bloat and vacuum lag. An index that speeds reads but doubles
  the import's write amplification may not be the right migration-time index.

## Disk and memory planning

Use `pg_total_relation_size`, actual average row widths and WAL generation from
a representative rehearsal to replace these provisional allowances:

| Category | Initial working allowance |
|---|---:|
| 150M events with indexes | 100–300 GB |
| Profiles, source records, identifiers and crosswalks | 50–150 GB |
| Gifts, enrichment, traits, memberships and audit history | 30–150 GB, strongly dependent on retained gifts/attributes |
| WAL, staging, sorting, index-build and migration headroom | At least another 1–2× the final database size |

Start capacity planning around **1–2 TB usable fast SSD/NVMe**, excluding
independent backups. Larger payloads, simultaneous copies of the event table or
long WAL retention can exceed this allowance. Require an alert and stop policy
well before free space drops below the largest remaining index/backfill batch.

For a 16-core database host, **64–128 GB RAM** is a starting planning range, not
a proven minimum. Budget per-query/per-worker sort memory across all concurrent
queries rather than setting a large global `work_mem`. Reserve capacity for the
operating-system cache, connection overhead and autovacuum. Use bounded import
spools and streamed seed/ground-truth processing.

## Rehearsal and release gates

1. Capture source export sizes, historical event counts and event payload widths.
2. Load a representative 500k slice with realistic historical fact density,
   including high-degree identities, refunds and late-arriving data.
3. Measure cold application queries during writes, not just warmed count caches.
4. Verify exact audited audiences, Master ID aliases/unmerge, import restart,
   consent/suppression and deletion across all tables and local artifacts.
5. Measure a full graph pass on the target 16-core machine before extrapolating.
6. Confirm backup restoration, disk/WAL alerts and migration cancellation.
7. Only then authorize the 8M run.

## Supported generation command — not executed

```sh
kinship seed --profiles 8000000 --scale large --as-of YYYY-MM-DD
```

This generates files only unless the explicit load option is supplied.
Choose the reference date and a capacity-checked output location first.
Do not run this command as part of ordinary CI or preview startup.
