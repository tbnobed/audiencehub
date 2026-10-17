#!/usr/bin/env python3
"""Resume an INTERRUPTED private all-five run; never import or repair data.

Parent runs this foreground controller with its managed background-task API.
Only normal worker stale-job reclamation may retry interrupted application work.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from full_load_acceptance import BACKEND, PG17, ROOT, resources, safe_directory, verify, write_json


TABLES = ("imports", "source_records", "profiles", "profile_merges", "gifts", "events",
          "profile_traits", "trait_snapshots", "trait_dirty_profiles",
          "dashboard_daily", "dashboard_event_daily", "dashboard_kpis")


def snapshot(engine):
    with engine.connect() as db:
        db.exec_driver_sql("SET TRANSACTION READ ONLY")
        db.exec_driver_sql("SET LOCAL statement_timeout='180s'")
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "counts": {table: db.scalar(text(f"SELECT count(*) FROM {table}")) for table in TABLES},
            "unattached": {table: db.scalar(text(
                f"SELECT count(*) FROM {table} WHERE profile_id IS NULL"))
                for table in ("source_records", "gifts", "events")},
            "unresolved": db.scalar(text("SELECT count(*) FROM source_records WHERE resolved_at IS NULL")),
            "active_profiles": db.scalar(text(
                "SELECT count(*) FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted")),
            "missing_active_traits": db.scalar(text(
                "SELECT count(*) FROM profiles p WHERE NOT p.is_deleted AND p.merged_into_id IS NULL "
                "AND NOT EXISTS(SELECT 1 FROM profile_traits t WHERE t.profile_id=p.id)")),
            "imports": [dict(r) for r in db.execute(text("SELECT * FROM imports ORDER BY id")).mappings()],
            "jobs": [dict(r) for r in db.execute(text(
                "SELECT id,type,status,payload,attempts,max_attempts,started_at,finished_at,"
                "heartbeat_at,progress FROM jobs ORDER BY id")).mappings()],
            "source_counts": [dict(r) for r in db.execute(text(
                "SELECT s.key,count(*) records FROM source_records sr JOIN sources s "
                "ON s.id=sr.source_id GROUP BY s.key ORDER BY s.key")).mappings()],
            "migrations": list(db.execute(text("SELECT version_num FROM alembic_version")).scalars()),
        }


def validate_checkpoint(before, fixture, manifest):
    from app.importer import SEED_SOURCES
    stats = json.loads((fixture / "generator_stats.json").read_text())["files"]
    imports = {row["filename"]: row for row in before["imports"]}
    if len(before["imports"]) != 5 or set(imports) != set(SEED_SOURCES):
        raise RuntimeError("Recovery requires exactly the five original imports")
    actual = {row["key"]: row["records"] for row in before["source_counts"]}
    expected = {}
    for filename, (source, _, _) in SEED_SOURCES.items():
        row = imports[filename]
        total = manifest["counts"][filename]
        invalid = stats[filename]["messiness_injected"].get("unrecoverable_row", 0)
        duplicate = stats[filename]["messiness_injected"].get("duplicate_row", 0)
        if (row["status"] != "completed" or row["file_sha256"] != manifest["sha256"][filename]
                or row["rows_total"] != total or row["rows_ok"] != total - invalid
                or row["rows_rejected"] != invalid
                or row["last_committed_record_number"] != total + 1
                or row["rows_deduplicated"] != duplicate):
            raise RuntimeError(f"Retained import checkpoint mismatch: {filename}")
        expected[source] = total - invalid - duplicate
    if actual != expected or sum(actual.values()) != manifest["record_count"]:
        raise RuntimeError("Retained unique source counts differ from fixture/checkpoints")
    if (before["counts"]["gifts"] != expected["giving_platform"]
            or before["counts"]["events"] != expected["five9"]):
        raise RuntimeError("Retained gift/event committed counts differ from fixture/checkpoints")
    failed = [j["id"] for j in before["jobs"]
              if j["status"] in ("failed", "cancelled") and j["type"] in
              ("import.run", "identity.resolve_batch", "traits.recompute")]
    if failed:
        raise RuntimeError(f"Terminal dependent jobs require parent review, not silent repair: {failed}")
    exhausted = [j["id"] for j in before["jobs"]
                 if j["status"] == "running" and j["attempts"] >= j["max_attempts"]]
    if exhausted:
        raise RuntimeError(f"Normal stale recovery would exhaust retry budget: {exhausted}")
    return expected


def job_observation(engine):
    with engine.connect() as db:
        db.exec_driver_sql("SET TRANSACTION READ ONLY")
        return [dict(row) for row in db.execute(text(
            "SELECT id,type,status,attempts,started_at,finished_at,progress "
            "FROM jobs ORDER BY id")).mappings()]


def phase_deltas(before, after, observations):
    old = {job["id"]: job for job in before["jobs"]}
    results = []
    for final in after["jobs"]:
        prior = old.get(final["id"], {})
        elapsed = (final.get("progress") or {}).get("phase_elapsed_ms", {})
        baseline = (prior.get("progress") or {}).get("phase_elapsed_ms", {})
        raw = {phase: (ms - baseline.get(phase, 0)) / 1000 for phase, ms in elapsed.items()}
        item = {
            "job_id": final["id"], "type": final["type"],
            "prior_attempts": prior.get("attempts", 0), "final_attempts": final["attempts"],
            "prior_progress": prior.get("progress"), "final_progress": final["progress"],
            "raw_cumulative_phase_delta_seconds": raw,
            "attempt_wall_seconds": (
                (final["finished_at"] - final["started_at"]).total_seconds()
                if final["finished_at"] and final["started_at"] else None),
            "inherited_open_interval_warning": bool((prior.get("progress") or {}).get("phase")),
        }
        # The old open phase includes downtime when begin_phase first closes it.
        # Capture the new resolver phase's initial baseline, so subtracting it
        # removes that inherited interval exactly, not by a guessed restart time.
        initial = next((j for obs in observations for j in obs["jobs"]
                        if j["id"] == final["id"] and j["attempts"] > prior.get("attempts", 0)
                        and (j.get("progress") or {}).get("phase") == "resolving"), None)
        if initial:
            start = initial["progress"]["phase_elapsed_ms"]
            item["resumed_phase_seconds"] = {
                phase: (ms - start.get(phase, 0)) / 1000 for phase, ms in elapsed.items()}
            item["resumed_phase_baseline"] = initial["progress"]
        elif not item["inherited_open_interval_warning"]:
            item["resumed_phase_seconds"] = raw
        else:
            item["resumed_phase_seconds"] = None
        results.append(item)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-run", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-approved", action="store_true", required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--timeout", type=int, default=14400)
    args = parser.parse_args()
    original = args.original_run.resolve()
    data = original / "postgres"
    if not original.is_relative_to(ROOT / ".local") or original.is_symlink():
        raise ValueError("Only retained workspace/.local benchmark clusters may be recovered")
    if args.output.exists():
        raise ValueError("Use a NEW recovery output directory; preserve all previous results")
    if (not data.is_dir() or data.is_symlink() or data.stat().st_uid != os.getuid()
            or (data / "PG_VERSION").read_text().strip() != "17"):
        raise ValueError("Retained cluster must be owned by this user and PostgreSQL 17")
    status = subprocess.run([str(PG17 / "pg_ctl"), "-D", str(data), "status"],
                            capture_output=True)
    if status.returncode != 3:
        raise RuntimeError("Cluster may already be running or status is uncertain; refusing recovery")
    # Additional process ownership check; never kill an unknown PID or remove pid files.
    for proc in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = proc.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, PermissionError):
            continue
        if "postgres" in command and str(data) in command:
            raise RuntimeError("A process already references the retained cluster")
    private = json.loads((original / "250k" / "private-env.json").read_text())
    url = make_url(private["DATABASE_URL"])
    socket = Path(str(url.query.get("host", "")))
    ready = json.loads((original / "private-ready.json").read_text())
    if (private.get("APP_ENV") != "test" or private.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1"
            or url.host or url.database != "full_load_250k" or url.username != "benchmark"
            or not str(socket).startswith("/tmp/kinship-bench-")
            or socket != Path(ready["socket"]) or data != Path(ready["data"])
            or Path(ready["pg_bin"]) != PG17):
        raise ValueError("Retained private environment/cluster/socket ownership contract mismatch")
    if socket.exists() and (socket.is_symlink() or socket.stat().st_uid != os.getuid()):
        raise ValueError("Private socket directory ownership mismatch")
    socket.mkdir(mode=0o700, exist_ok=True)
    fixture = args.fixture.resolve()
    manifest = verify(fixture)
    output = safe_directory(args.output)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG") and k != "DATABASE_URL"}
    env.update(private, PYTHONPATH=str(BACKEND), TMPDIR=str(output),
               WORKER_CONCURRENCY=str(args.workers), WORKER_STOP_GRACE_SECONDS="20",
               PATH=str(PG17) + os.pathsep + os.environ["PATH"])
    # Imported modules must also see only the verified disposable environment.
    os.environ.update(env)
    report = {"completed": False, "run_kind": "INTERRUPTED/RESUMED",
              "uninterrupted_benchmark_valid": False, "original_run": str(original),
              "people": manifest["people"], "resources": resources()}
    write_json(output / "result.json", report)
    control = subprocess.check_output([str(PG17 / "pg_controldata"), str(data)], text=True)
    (output / "pre-start-control.txt").write_text(control)
    started = time.monotonic()
    server = worker = monitor = engine = None
    observations = []

    def stop_requested(signum, frame):
        raise KeyboardInterrupt(f"Recovery controller interrupted ({signum})")

    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    try:
        with (output / "postgres.log").open("w") as pglog:
            # Foreground PostgreSQL belongs to the managed controller task.
            server = subprocess.Popen([
                str(PG17 / "postgres"), "-D", str(data), "-k", str(socket), "-h", "",
                "-p", "5432", "-c", "shared_buffers=256MB", "-c", "work_mem=8MB",
                "-c", "max_connections=40", "-c", "max_wal_size=1GB"],
                stdout=pglog, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 600
        while True:
            if server.poll() is not None:
                raise RuntimeError("Private PostgreSQL crash recovery failed; retained files untouched")
            ready_check = subprocess.run([str(PG17 / "pg_isready"), "-h", str(socket),
                                          "-U", "benchmark", "-d", "full_load_250k"],
                                         capture_output=True)
            if ready_check.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Private PG crash recovery startup exceeded 600s")
            time.sleep(1)
        engine = create_engine(private["DATABASE_URL"])
        with engine.connect() as db:
            db.exec_driver_sql("SET TRANSACTION READ ONLY")
            if Path(db.scalar(text("SHOW data_directory"))).resolve() != data:
                raise RuntimeError("Connected server is not retained private PGDATA")
            report["postgres_version"] = db.scalar(text("SELECT version()"))
            report["system_identifier"] = str(db.scalar(text("SELECT system_identifier FROM pg_control_system()")))
        before = snapshot(engine)
        write_json(output / "pre-recovery-state.json", before)
        report["validated_source_counts"] = validate_checkpoint(before, fixture, manifest)
        report["crash_recovery_and_validation_seconds"] = time.monotonic() - started
        write_json(output / "result.json", report)
        ids = [row["id"] for row in before["imports"]]
        # No imports are submitted, no profiles inserted, no jobs altered here.
        # Existing worker maintenance invokes normal requeue_stale on expired work.
        with (output / "worker.log").open("w") as wlog, (
                output / "load-status.txt").open("w") as slog:
            worker = subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=BACKEND,
                                      env=env, stdout=wlog, stderr=subprocess.STDOUT,
                                      start_new_session=True)
            monitor = subprocess.Popen([
                sys.executable, "-m", "app.cli", "load-status", "--wait",
                "--timeout", str(args.timeout),
                *[part for iid in ids for part in ("--import-id", str(iid))]],
                cwd=BACKEND, env=env, stdout=slog, stderr=subprocess.STDOUT)
            resume_started = time.monotonic()
            with (output / "job-observations.jsonl").open("w") as evidence:
                while monitor.poll() is None:
                    if worker.poll() is not None or server.poll() is not None:
                        raise RuntimeError("Private worker or PostgreSQL exited during recovery")
                    if time.monotonic() - resume_started > args.timeout + 60:
                        raise TimeoutError("Resumed pipeline monitoring deadline exceeded")
                    item = {"captured_at": datetime.now(timezone.utc).isoformat(),
                            "jobs": job_observation(engine)}
                    observations.append(item)
                    evidence.write(json.dumps(item, default=str) + "\n")
                    evidence.flush()
                    time.sleep(5)
            report["resumed_pipeline_wall_seconds"] = time.monotonic() - resume_started
            report["final_load_status_exit"] = monitor.returncode
        after = snapshot(engine)
        write_json(output / "post-recovery-state.json", after)
        report["phase_attempt_evidence"] = phase_deltas(before, after, observations)
        report["counts"] = after["counts"]
        report["unattached"] = after["unattached"]
        report["active_profiles"] = after["active_profiles"]
        # Validate unchanged committed imported data a second time.
        validate_checkpoint(after, fixture, manifest)
        if monitor.returncode:
            raise RuntimeError("Resumed real load-status did not PASS; no repair attempted")
        if (after["unresolved"] or after["missing_active_traits"]
                or after["counts"]["trait_dirty_profiles"]
                or not after["counts"]["trait_snapshots"]
                or not after["counts"]["dashboard_daily"]
                or not after["counts"]["dashboard_event_daily"]
                or not after["counts"]["dashboard_kpis"]):
            raise RuntimeError("Normal resumed pipeline has unresolved/missing trait/history/rollup work")
        with (output / "identity-accuracy.txt").open("w") as accuracy:
            accuracy_started = time.monotonic()
            result = subprocess.run([
                sys.executable, str(Path(__file__).with_name("identity_acceptance.py")),
                "--ground-truth", str(fixture / "ground_truth.json"),
                "--metrics-json", str(output / "identity-accuracy-metrics.json")],
                cwd=BACKEND, env=env, stdout=accuracy, stderr=subprocess.STDOUT)
            report["accuracy_seconds"] = time.monotonic() - accuracy_started
            report["accuracy_exit"] = result.returncode
            if result.returncode:
                raise RuntimeError("Resumed ground-truth accuracy failed")
        report["completed"] = True
        report["correctness_outcome"] = "PASS_AFTER_INTERRUPTION_AND_RESUME"
    except BaseException as exc:
        report["failure_type"] = type(exc).__name__
        if isinstance(exc, (RuntimeError, TimeoutError)):
            report["failure_reason"] = str(exc)
        # Connection exceptions can contain credentials: do not serialize their text.
        raise
    finally:
        for child in (monitor, worker):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        if worker:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if engine:
            engine.dispose()
        if server is not None and server.poll() is None:
            subprocess.run([str(PG17 / "pg_ctl"), "-D", str(data), "-m", "fast",
                            "-w", "-t", "120", "stop"], check=False, capture_output=True)
            try:
                server.wait(timeout=130)
            except subprocess.TimeoutExpired:
                report["cleanup_warning"] = "Private server did not stop; parent intervention required"
        report["resumed_controller_wall_seconds"] = time.monotonic() - started
        report["resources_final"] = resources()
        write_json(output / "result.json", report)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not print driver exception details or private connection settings.
        print(f"Private recovery stopped ({type(exc).__name__}); "
              "preserve PGDATA and review the recovery result evidence.", file=sys.stderr)
        raise SystemExit(1)
