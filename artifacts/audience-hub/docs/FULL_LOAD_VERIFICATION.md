# Full five-source load verification

## Final outcome: PASS after interruption and normal worker recovery

The genuine **250,000-person, medium-density, five-source pipeline completed**.
Evidence: `.local/full-load-verification/run-03/recovery-01/result.json`
reports `completed=true`, `correctness_outcome=PASS_AFTER_INTERRUPTION_AND_RESUME`,
`final_load_status_exit=0`, and `accuracy_exit=0`. Ground-truth metrics in
`identity-accuracy-metrics.json` pass the existing documented M3 rules.

**This is a correctness acceptance after recovery, not an uninterrupted
performance benchmark.** The original interrupted result remains untouched.
There were no clones, regenerated fixtures, repeat imports, manual profile
inserts, direct resolver calls, forced job completions, or CLI trait repairs.
All imports came through the real seed-load importer/jobs; normal worker
dispatch completed identity, attachments, traits/history and dashboard rollups.

Recovery established a material correction to the interruption-time hypothesis:
identity and attachment had **already committed successfully before the restart**.
Job 9 stayed at attempt 1 and was not rerun. Jobs 10 (batch full traits) and 11
(scheduled dirty traits) had stale running leases and were reclaimed through
the normal worker maintenance path, succeeding at attempt 2. The recovered
pre-worker snapshot already contained the committed profiles, attachments and
trait/rollup rows; these were validated again after dependency jobs completed.

### Final committed counts and coverage

| Measure | Committed result |
|---|---:|
| Completed imports | 5 |
| Unique source records | 3,390,190 |
| CRM / ESP / enrichment source records | 250,000 / 125,000 / 250,000 |
| Gift source records / gifts | 2,675,657 / 2,675,657 |
| Event source records / events | 89,533 / 89,533 |
| Active profiles / profile traits | 246,438 / 246,438 |
| Recorded existing-profile merges | 0 |
| Unresolved source records / missing active traits / dirty traits | 0 / 0 / 0 |
| Unattached gifts | 0 |
| Unattached events and corresponding source records | 493 / 493 |
| Trait snapshot aggregate rows | 114 |
| Dashboard daily / event daily / KPI rows | 145,041 / 7,304 / 13 |
| Multisource profiles | 245,423 (99.5881% of active profiles) |

The 114 snapshot rows are **month/status aggregate rows**, not 114 profiles.
The real history backfill path ran; distinct-month coverage was not separately
queried after shutdown.

All five import hashes, exact generated/handled/rejected/deduplicated counts,
last committed record-number checkpoints, source counts, gifts and events
matched the retained fixture at recovery and after completion. Rejected rows
were exactly the five deliberately injected unrecoverable rows (one per CSV).
Deduplicated rows were CRM 2,476, ESP 735, and one each for gifts/events/enrichment:
3,214 total. These are distinct from the **zero existing-profile merge events**.

Zero merge events does not mean identity resolution was bypassed: this was a
fresh graph, where matching records can be assigned directly to one newly
created component profile without first creating two profiles and merging
them. The 245,423 multisource profiles and ground-truth pair results demonstrate
cross-source consolidation. Conversely, zero merge rows is not proof of perfect
identity quality; eight non-household mixed-person profiles remain.

### Actual ground-truth accuracy and documented thresholds

| Metric | Result / denominator |
|---|---|
| Active profile count difference | −3,562 / 250,000 = −1.4248% |
| M3 count threshold | absolute difference ≤2%: **PASS** |
| Mixed-person profiles | 4,930 / 246,438 |
| Intentional household profiles | 4,922 |
| Other mixed-person profiles | 8 / 246,438 = 0.0032463% |
| M3 mixed-profile threshold | non-household fraction <1%: **PASS** |
| Matched ground-truth records | 3,389,697 / 3,390,190 = 99.985458% |
| Missing truth records / wholly missing people / unknown actual labels | 493 / 0 / 0 |
| Strict pair precision | 79,131,638 / 80,027,252 = **98.880864%** |
| Strict pair recall, including unmatched truth | 79,131,638 / 79,161,894 = **99.961780%** |
| Strict false-positive / false-negative pairs | 895,614 / 30,256 |
| Intentional-household / other false-positive pairs | 894,585 / 1,029 |
| Supplemental household-exempt precision | 80,026,223 / 80,027,252 = **99.998714%** |

There is **no invented pairwise PASS threshold**; the metrics JSON records
`pairwise_threshold=null` and cites `docs/MILESTONES.md M3` for the actual two
acceptance thresholds. Raw precision is shown alongside the household-adjusted
diagnostic, not replaced by it.

All CRM, ESP, gift and enrichment truth keys matched. The 493 unmatched truth
keys are Five9 records (89,040 / 89,533 matched), and remain included in recall.
They are **not** dismissed as the five injected rejected rows. The load-status
contract distinguishes unattached events from unresolved contact work; final
status is PASS with zero unresolved contacts and these event exceptions visible.

There are 1,368 split true people: the helper classified 1,085 with junk CRM email
and a distinct ESP email, 266 with malformed CRM phone and a distinct ESP email,
11 with junk CRM email and no matched ESP, and six with malformed CRM email.
The retained report prints evidence for five of the eight mixed-person cases:
shared phone identifiers had low source populations, below the resolver's
high-cardinality auto-block threshold. Household annotations were absent.
These remain real measured identity errors, not silently waived exceptions.

### Measured timings: separate attempts, phases, and elapsed wall

| Timing | Observed seconds | Interpretation |
|---|---:|---|
| Small preflight complete harness | 36.466 | 500-person real pipeline + verification |
| Small preflight import/dependency drain | 34.393 | Includes polling/queue gaps |
| Large imports, stored start-to-last-finish span | 843.576 | Concurrent imports; first queued-to-last-finish 866.417 |
| Sum of individual import durations | 1,011.250 | Job-seconds, **not wall**; per-file table below |
| Identity resolving | 267.993 | Original successful attempt 1 |
| Gift/event/reference attaching | 727.071 | Original successful attempt 1 |
| Identity job total wall | 995.467 | 03:25:06.575–03:41:42.041 UTC |
| Original full-job traits phase | 63.903 | Before interruption; not a fresh complete-run phase total |
| Recovery startup + checkpoint/count validation | 48.767 | No repeat import or identity work |
| Resumed full-job traits | 41.075 | Attempt 2, observation-baseline corrected |
| Resumed full-job rollups | 186.577 | Attempt 2, observation-baseline corrected |
| Resumed full-traits job wall | 228.748 | Includes coordination/commit overhead |
| Scheduled dirty-traits job attempt wall | 228.732 | Concurrent; largely waits on full recompute lock |
| Resumed pipeline barrier wall | 235.377 | Includes worker reclamation and status polling |
| Bounded all-truth accuracy helper | 409.810 | Verification overhead, not application load |
| Entire recovery controller wall | 695.697 | Startup, validation, resumed jobs, accuracy, cleanup |
| Original first import start to final dependent completion | 3,111.5 | **51.86 min including interruption/recovery gap** |

Final load-status reports PASS and exit 0. Its raw cumulative job telemetry
shows rollups 1,154.665 seconds, which **includes the inherited open phase and
downtime** and must not be used as a fresh rollup duration. Exact correction is
available in `job-observations.jsonl`: when full job 10 restarted its traits
phase, the recorded baseline was traits 63,902.797 ms, rollups 968,087.393 ms.
Final totals were 104,977.463 ms and 1,154,664.804 ms. Subtraction yields
41.074666 s traits and 186.577411 s rollups. Dirty job 11 similarly had an
inherited 1,014,143.175 ms baseline and a 227.676 s new interval, including lock
wait. Do not sum the concurrent full and dirty jobs as serial work.

The generic recovery JSON intentionally leaves resumed phase fields unknown
for these jobs because its automatic baseline matcher targets resolver restarts;
the figures above are derived from the explicit **trait-job** opening observations,
not guessed by subtracting approximate outage time.

After this measurement, retry telemetry was corrected to bind phase timers to
the actual attempt and start time, reset open intervals on retry, and treat
legacy unscoped timing as unknown. A 27-test isolated PostgreSQL status suite
passed, including a one-hour retry gap. Batch completion timestamps now use
`clock_timestamp()` rather than transaction-start `now()`; a delayed-transaction
regression passed. These reporting corrections do not alter the measured
identity, attachment or trait results above. Attachment timing includes its
post-attachment statistics refresh.

### Resources, retained evidence, and production projection

Actual quota `cpu.max=400000 100000`, `nproc=4`, memory cap 8 GiB, two process
workers, PostgreSQL 17.5. Observed recovery-environment memory peak was
6,953,414,656 bytes (**6.476 GiB**); no cgroup OOM event was recorded in this
recovery environment. This is an environment peak including cache, not exclusive
application RSS. Prior run disk sampling reached ~12 GiB; final retained PGDATA
is ~8.7 GiB, fixtures ~799 MiB, recovery reports ~244 KiB. The controller stopped
private PostgreSQL and workers; pinned `pg_ctl status` confirms no server running.
PGDATA and all evidence remain retained, ignored by Git/deployment.

For **500k on 16 production cores**, use the supplied observed **31-minute gift
import** rather than divide this four-core import wall by four. A transparent
planning sensitivity range is:

| Phase | 500k planning range | Assumption, not benchmark |
|---|---:|---|
| Gifts/import critical path | ~31 min | Supplied production observation; other imports overlap |
| Identity resolution | 4.5–11.2 min | 1.0–2.5× measured 250k phase |
| Attachment | 12.1–30.3 min | 1.0–2.5× measured phase; SQL/I/O-bound |
| Traits | 0.7–1.7 min | 1.0–2.5× resumed phase |
| Rollups | 3.1–7.8 min | 1.0–2.5× resumed phase; cache/history state differs |
| Queue, import tail, statistics and other overhead | 2–5 min | Unmeasured production allowance |

Rounded total: **about 55–90 minutes**, as a conditional planning range, **not a
statistical confidence bound or SLA**. Its optimistic end assumes greater
capacity offsets doubled volume; its upper scenario allows doubled volume plus
25% extra SQL/I/O cost. Nonlinear joins, cold history backfill, resource
contention, or slower storage can exceed it. No assumption makes every phase
four times faster. If all-truth acceptance verification is also in the time
budget, its largely serial helper adds roughly 14–21 minutes at doubled volume
under a separate 2–3× sensitivity assumption.

**Under 60 minutes is possible only near the optimistic application-load
scenario; it is not demonstrated or certified.** Full production measurement
is required. The recovered correctness PASS and the 51.86-minute elapsed
250k timeline must not be presented as proof of an uninterrupted 500k SLA.

## Historical interruption snapshot (superseded by recovery evidence above)

At approximately 2026-10-17 03:50 UTC the blocking wait tool disconnected.
On reconnect, neither the controller nor its benchmark processes remained;
pinned PG17 `pg_ctl status` confirmed **no server running**. PG control data
still said `in production`, indicating an unclean stop. The cgroup memory peak
had reset to below 1 GiB, consistent with an environment restart. The exact
restart cause is unknown; current zero OOM counters do not establish whether
the previous environment suffered an OOM.

At that point the retained large result was `completed=false`, without final phase
totals or wall completion. No large final-load-status or accuracy metrics file
was produced. Therefore full 250k acceptance, accuracy, final committed
attachments/traits/rollups, and the <60-minute target are **unverified**.
This is infrastructure interruption, not evidence of a completed application
failure or a successful load. No restart, second large run, database mutation,
or cleanup was attempted after the interruption.

Last read-only committed import snapshot before loss:

| CSV | Total rows | OK | Rejected | Import duration |
|---|---:|---:|---:|---:|
| CRM | 252,477 | 252,476 | 1 | 55.627 s |
| Gifts | 2,675,659 | 2,675,658 | 1 | 853.795 s |
| ESP | 125,736 | 125,735 | 1 | 29.675 s |
| Events | 89,535 | 89,534 | 1 | 30.353 s |
| Enrichment | 250,002 | 250,001 | 1 | 41.800 s |

These are import handling counters, **not** unique committed source-record
counts; injected duplicates are separately deduplicated. Import durations
overlap and must not be summed as wall clock. The real identity job began
03:25:06 UTC, recorded 267.993 s resolving, and entered attaching at 03:29:34 UTC.
The last directly queried job snapshot was still attaching. No final attaching
duration or post-import traits/rollup duration can be certified.

Environment: quota 4 CPUs (`400000 100000`), nproc 4, 8 GiB cap, two process
workers, PostgreSQL 17.5. Sampled pre-interruption cgroup peak reached
6,898,507,776 bytes (~6.42 GiB); this is an observed environment high-water mark,
not a guaranteed final or workload-exclusive peak. Private PGDATA was sampled
at up to approximately 12 GiB during the run; retained directory was 8.3 GiB
after reconnect (temporary/WAL growth can fluctuate). Original fixtures,
preflight evidence, partial large result, and PGDATA remain under `.local`.
No integrity/recoverability claim is made for the unclean cluster.

### Projection limitation before recovery completed

Using the supplied production observation of approximately **31 minutes for
gift import** is more defensible than scaling this environment's gift duration
by core count. However, attachment and final traits/history/rollups were not
measured to completion. Consequently no defensible closed end-to-end range or
sub-60-minute prediction follows from this interrupted run. Production would
have roughly 29 minutes remaining after that gift-import critical path for
all non-overlapped dependency work; whether it fits is unverified. More cores
do not make SQL, I/O, atomic attachment, and snapshot work uniformly four times
faster. Preserve these partial measurements rather than fabricate missing
phase timings.

### Targeted recovery controller (subsequently executed successfully)

After the parent's attachment-plan fix and explicit stable-code approval, run
under its managed background-task lifecycle:

```sh
python artifacts/audience-hub/scripts/full_load_recovery.py \
  --original-run .local/full-load-verification/run-03 \
  --fixture .local/full-load-verification/fixtures/250k-medium \
  --output .local/full-load-verification/run-03/recovery-01 \
  --start-approved --workers 2 --timeout 14400
```

This is **not a fresh full run**. It verifies the retained cluster is PG17,
owned by the current user, not already running, and tied to the original private
socket and `full_load_250k` credentials. It starts the pinned PG17 server in
foreground under the controller, permitting normal crash recovery without
cloning, resetting, wiping, regenerating fixtures, or resubmitting imports.
Recovery errors stop execution and preserve PGDATA.

Before starting workers it writes `pre-recovery-state.json`, checks all five
completed imports against manifest hashes, row totals/OK/rejected/dedup counts,
and final durable record-number checkpoints; then checks exact per-source
unique counts and gifts/events against generator statistics. A failed/terminal
dependent job or exhausted stale-retry budget stops for parent review.
There are no manual job-state updates: normal worker stale-heartbeat maintenance
must reclaim the interrupted identity job.

The original `250k/result.json` is never overwritten. New results are explicitly
`INTERRUPTED/RESUMED`, with `uninterrupted_benchmark_valid=false`. Read-only
job observations preserve prior attempts and phase telemetry. Old open phases
can accrue downtime on their next phase transition; raw cumulative deltas are
therefore labeled as contaminated. When the new resolver phase's opening
baseline is observed, subtracting that baseline yields resumed phase durations
without falsely counting downtime. Otherwise phase duration stays unknown,
while the resumed attempt wall interval is reported separately.

Final validation uses the real load-status barrier, committed counts, normal
worker-produced traits/history/rollups, and the bounded ground-truth helper
with documented M3 thresholds. A successful result is
`PASS_AFTER_INTERRUPTION_AND_RESUME`, never an uninterrupted throughput pass.
The controller stops its workers/private server, preserving original fixtures,
PGDATA, before/after state, status output, accuracy JSON, and attempt evidence.

## Archived preparation plan — not acceptance evidence

At preparation, acceptance had **not run**. Parent stable-code confirmation, the final phase
contract, and a successful small preflight are required before the 250k run.
No live application database is permitted.

The harness is `scripts/full_load_acceptance.py`. `prepare` reuses the real
deterministic generator with 250,000 requested people, medium density, seed
20250308, all five CSVs, generator statistics, and ground truth. It preserves
fixtures across failures and records SHA-256 checksums. Outputs must be under
workspace `.local`, already excluded from both Git and deployment.

`run` uses `app.seed.load_generated_files`, the actual `seed --load` import hook,
then verifies normal queued workers produced traits, history and dashboard
rollups **without** CLI trait/history/template repairs. It does
not inject rows, call a resolver directly, cancel dependent jobs, or substitute
a contact-only benchmark. Normal process workers must be supplied by the parent
supervisor. Only test-mode Unix socket databases named `full_load_*` are accepted.
The 250k invocation additionally requires a successful small-run JSON report.

Example preparation from workspace root:

```sh
python artifacts/audience-hub/scripts/full_load_acceptance.py prepare \
  --directory .local/full-load-verification/fixtures/250k-medium --people 250000
```

Prepare a separate 500-person medium fixture for the post-greenlight preflight.
Generation is independent of database credentials. Never regenerate intact
fixtures just to restart a failed private server.

## Archived resource budget and infrastructure plan

Observed preparation environment: `cpu.max = 400000 100000`, `nproc = 4`,
cgroup memory maximum 8,589,934,592 bytes (8 GiB), no swap. Initial host-visible
memory availability was about 3.8 GiB. PostgreSQL on PATH was **17.5**.
These are four quota-equivalent CPUs, not a 16-core production measurement.

Plan one private cluster, no full-data clones. Place PGDATA, WAL, uploads,
fixtures, SQLite accuracy scratch, and reports under `.local`; only the short
Unix socket goes under `/tmp/kinship-bench-*`. Initial workspace `df` advertised
251 GiB free, which is **not proof of an adequate platform quota**. Reserve a
provisional 15–25 GiB envelope and monitor actual growth; stop and report quota
failure rather than delete PostgreSQL relation files. Keep prepared fixtures and
results outside PGDATA. Four worker processes plus PostgreSQL compete for the
same four CPU quota and 8 GiB memory; begin preflight conservatively and verify
memory before increasing concurrency.

Use a persistent supervisor for generation and long-running private PostgreSQL,
workers, and acceptance. Shell backgrounding alone does not survive tool
timeouts reliably. Pin the absolute PostgreSQL 17 binary directory across init,
start, resume, and stop. Explicitly stop the private server after evidence
collection. The controller starts only private benchmark infrastructure, never
application workflows.

### One-command private controller (parent persistent background task)

Run this in the parent's `ShellExec.run_in_background` / Monitor lifecycle, not
as a detached child of a synchronous shell:

```sh
python artifacts/audience-hub/scripts/full_load_acceptance.py controller \
  --directory .local/full-load-verification/fixtures/250k-medium \
  --preflight-directory .local/full-load-verification/fixtures/500-medium \
  --output .local/full-load-verification/run-01 \
  --greenlight-file .local/full-load-verification/stable-code-approved \
  --workers 2 --timeout 14400
```

The greenlight file must not exist at startup. The controller initializes and
starts pinned PG17, writes `run-01/private-ready.json`, and waits without running
imports or workers. Once final core tests/phase instrumentation are stable and
fixture generation is complete, the parent writes exactly
`STABLE_CODE_APPROVED` to that file. It then migrates a fresh small database,
runs process workers and real small acceptance, and only on success creates the
fresh 250k database and repeats. There are no clones; the small database is kept
for evidence. Workers and PostgreSQL are stopped in cleanup, while PGDATA and
fixtures remain. A new controller output directory is required for a retry.

At preparation the controller had not been launched by this subagent: its exposed
shell tool lacks the modern background-task arguments. Parent owns that launch.
Never duplicate the already parent-managed 250k fixture generation task.

### Restart recovery and approved immediate launch

Following the workspace restart, pinned PG17 `pg_ctl status` reported **no
server running** for run-01. Its 39 MiB PGDATA is retained untouched. Control
data still says `in production`, so it was not a clean shutdown; a checkpoint
exists but recoverability has **not** been tested or certified. No acceptance
database had been loaded. Prefer a fresh run-02 rather than treating this
interrupted infrastructure startup as a trustworthy completed baseline.

The retained 250k medium fixture passed all manifest SHA-256 checks without
regeneration. Its five row counts are 252,477 CRM; 2,675,659 gifts; 125,736 ESP;
89,535 events; 250,002 enrichment. Ground truth contains 3,390,190 mappings.
The fixture directory occupies approximately 799 MiB.

After explicit parent stable-code greenlight, launch this via the managed
background-task lifecycle (not before):

```sh
python artifacts/audience-hub/scripts/full_load_acceptance.py controller \
  --directory .local/full-load-verification/fixtures/250k-medium \
  --preflight-directory .local/full-load-verification/fixtures/500-medium \
  --output .local/full-load-verification/run-02 \
  --start-approved --workers 2 --timeout 14400
```

`--start-approved` is an explicit approval alternative to a waiting greenlight
file, not automatic approval inferred from stale files. No cross-restart
process survival is claimed. For a restart during actual loading, preserve
PGDATA/results and assess recovery separately; the controller intentionally
refuses to silently resume partly loaded data or overwrite existing PGDATA.
Rerun on a new disposable database with the same checksummed fixtures rather
than misreporting a partial/resumed run as a clean end-to-end measurement.

## Original evidence checklist

Retain committed counts for imports, source records, profiles, merges, gifts,
events, traits, snapshots and dirty profiles; each import's actual row outcomes;
each job's start/finish timestamps; aggregate pipeline and CLI history timings;
end-to-end wall clock; unresolved/unattached counts; and final real CLI
`load-status` output and exit code. The harness reuses `identity_acceptance.py`
for ground-truth precision/recall, multisource profiles and false-merge evidence.
Its successful execution alone is not an accuracy pass: review that report.

The status worker's final phase-count contract needed incorporation before the
expensive run so attachment, resolution, trait recompute and rollup phases are
reported individually rather than hidden behind one combined duration.
Concurrent job durations overlap; do not add them as if they were elapsed time.
Reject an apparently fast run that skipped records or left dependency work.
Explain injected bad-row rejections and ambiguous/shared-household merges.

## Original projection restriction before measurements

Before measurements, no runtime prediction or under-60-minute certification was supported.
After measurement, project 500k on 16 cores using measured phase behavior and an
explicit range: separate serial/I/O/SQL-bound work from demonstrably parallel
worker work, include doubled-volume identity/trait/history costs and memory/I/O
uncertainty. Never divide the entire four-core wall clock by four. The <60-minute
target remains unverified without representative production-scale measurement.

## Accuracy-helper memory correction and acceptance rules

The existing accuracy entrypoint now streams its split diagnostics in person
order and mixed-profile diagnostics in profile order instead of fetching every
matched record into Python lists/dictionaries. Its ground-truth reader was
already streaming in 10,000-record batches. SQLite temporary storage is FILE,
with a 32 MiB page-cache target. The full truth and aggregate intermediate tables
remain on workspace disk; Python diagnostic state scales with a single
person/profile group plus retained false-merge cases, not all gift records.
Existing diagnostic meanings and household exemptions are unchanged.

The **documented M3 rules**, from `docs/MILESTONES.md`, are the only numerical
PASS/FAIL thresholds: absolute profile count difference at most 2% of true
people, and non-household mixed-person profiles strictly below 1% of active
profiles. The helper emits that explicit outcome and exits 2 on failure.
It also records a metrics dictionary (with thresholds) in stdout and, for this
isolated harness, `identity-accuracy-metrics.json` under the run's TMPDIR.
An M3 PASS is not a claim that missing records or raw pairwise errors vanish.

Pairwise metrics are supplementary, with no invented pairwise pass threshold.
Pairs are weighted by unique `(source_key, external_id)` keys, not CSV duplicate
lines. Strict recall's denominator includes **all** ground-truth person pairs,
including records never matched to an active profile. Precision's denominator
is predicted same-profile pairs with both labels known; unknown-label records
and affected pairs are separately disclosed. Raw cross-person false positives
are separated into intentional whole-profile shared-household cases and other
false positives. Household-exempt precision is supplementary, not a replacement
for raw precision. Coverage includes per-source matched/unmatched records,
people with no match, and injected unrecoverable seed-row counts.

Verification before large accuracy: three standalone SQLite regression tests
passed (independent brute-force pair oracle, missing records/people, unknown
labels, household exemptions, split diagnostics, empty denominators), plus the
three existing backend acceptance-helper tests. On the actual committed small
preflight identities, old Git-baseline and new streaming diagnostics matched
**exactly**: zero non-household false-merge cases; three split people.

Read-only small identity evidence (not yet full preflight completion): 6,484
truth records, 6,482 matched, two unmatched, no unknown actual records, no
entirely unmatched people, 490 multisource profiles. Strict pair precision
148,060/149,789 = 0.9884570963; recall 148,060/148,076 = 0.9998919474.
All 1,729 cross-person predicted pairs were intentional household cases;
household-exempt precision was 1.0. The two unmatched records remain explicitly
in recall/coverage and are not silently classified as invalid-row exceptions.
