#!/usr/bin/env python3
"""All-five real seed-load acceptance. Infrastructure is owned by the supervisor.

Preparation never opens a database. Run requires explicit greenlight, a private
socket database, and a fresh migrated schema. Never substitutes direct resolution.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time

BACKEND = Path(__file__).resolve().parents[1] / "backend"
ROOT = BACKEND.parents[2]
sys.path.insert(0, str(BACKEND))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def resources():
    return {
        "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
        "nproc": subprocess.check_output(["nproc"], text=True).strip(),
        "memory_max": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
        "memory_current": Path("/sys/fs/cgroup/memory.current").read_text().strip(),
    }


def safe_directory(path):
    path = path.resolve()
    if not path.is_relative_to(ROOT / ".local"):
        raise ValueError("Generated evidence must remain in workspace/.local (git/deploy ignored)")
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare(args):
    from app.seed import CSV_FILES, generate_seed
    output = safe_directory(args.directory)
    manifest = output / "fixture-manifest.json"
    if manifest.exists():
        metadata = json.loads(manifest.read_text())
        if metadata["people"] != args.people:
            raise ValueError("Existing fixture has a different population")
        verify(output)
        print("Verified retained fixture; did not regenerate.")
        return
    if any(output.glob("*.csv")):
        raise ValueError("Incomplete fixture retained; use a new directory, do not overwrite evidence")
    started = time.perf_counter()
    result = generate_seed(profiles=args.people, scale="medium",
                           random_seed=20250308, output_dir=output)
    hashes = {}
    for name in (*CSV_FILES, "ground_truth.json"):
        with (output / name).open("rb") as stream:
            hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    write_json(manifest, {
        "people": args.people, "scale": "medium", "random_seed": 20250308,
        "counts": result["counts"], "record_count": result["record_count"],
        "sha256": hashes, "generation_seconds": time.perf_counter() - started,
        "resources": resources(),
    })
    print(manifest, flush=True)


def verify(directory):
    metadata = json.loads((directory / "fixture-manifest.json").read_text())
    for name, expected in metadata["sha256"].items():
        with (directory / name).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"Fixture checksum mismatch: {name}")
    return metadata


def run(args):
    if not args.parent_greenlight:
        raise ValueError("Wait for parent stable-code greenlight and small preflight")
    directory = safe_directory(args.directory)
    output = safe_directory(args.output)
    metadata = verify(directory)
    from sqlalchemy.engine import make_url
    url = make_url(os.environ["DATABASE_URL"])
    if (os.environ.get("APP_ENV") != "test"
            or os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1"
            or url.host
            or not str(url.query.get("host", "")).startswith("/tmp/kinship-bench-")
            or not str(url.database).startswith("full_load_")):
        raise ValueError("Only explicitly named disposable full_load_ socket databases are allowed")
    if metadata["people"] >= 250_000:
        if not args.preflight_report:
            raise ValueError("250k requires a completed small preflight report")
        preflight = json.loads(args.preflight_report.read_text())
        if not preflight.get("completed") or preflight["people"] >= 250_000:
            raise ValueError("Invalid small preflight evidence")
    from sqlalchemy import text
    from app.db import engine
    with engine.connect() as db:
        if db.scalar(text("SELECT count(*) FROM imports")) or db.scalar(
                text("SELECT count(*) FROM source_records")):
            raise ValueError("Fresh disposable schema required; do not reuse or clone loaded data")
        version = db.scalar(text("SELECT version()"))
    from app.seed import CSV_FILES, load_generated_files
    # This is the exact hook used by seed --load, preserving prepared fixtures.
    # Normal process workers must already be running under the parent supervisor.
    report = {"completed": False, "people": metadata["people"],
              "resources": resources(), "postgres_version": version,
              "phases_seconds": {}, "pipeline": "seed.load_generated_files + normal worker jobs only"}
    write_json(output / "result.json", report)
    started = time.perf_counter()
    try:
        begin = time.perf_counter()
        ids = load_generated_files({name: directory / name for name in CSV_FILES})
        report["phases_seconds"]["imports_and_worker_dependency_drain"] = time.perf_counter() - begin
        with (output / "final-load-status.txt").open("w") as status:
            # Use the real public CLI to avoid depending on the evolving emit contract.
            check = subprocess.run(
                [sys.executable, "-m", "app.cli", "load-status",
                 *[item for iid in ids for item in ("--import-id", str(iid))]],
                cwd=BACKEND, stdout=status, stderr=subprocess.STDOUT)
        report["final_load_status_exit"] = check.returncode
        with engine.connect() as db:
            report["counts"] = {
                table: db.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in ("imports", "source_records", "profiles", "profile_merges",
                              "gifts", "events", "profile_traits", "trait_snapshots",
                              "trait_dirty_profiles", "dashboard_daily",
                              "dashboard_event_daily", "dashboard_kpis")
            }
            report["unattached"] = {
                table: db.scalar(text(f"SELECT count(*) FROM {table} WHERE profile_id IS NULL"))
                for table in ("source_records", "gifts", "events")
            }
            report["imports"] = [dict(row) for row in db.execute(text(
                "SELECT * FROM imports ORDER BY id")).mappings()]
            report["jobs"] = [dict(row) for row in db.execute(text(
                "SELECT id,type,status,progress,created_at,started_at,finished_at "
                "FROM jobs ORDER BY id")).mappings()]
            report["active_profiles"] = db.scalar(text(
                "SELECT count(*) FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted"))
            report["unresolved"] = db.scalar(text(
                "SELECT count(*) FROM source_records WHERE resolved_at IS NULL"))
            report["missing_active_traits"] = db.scalar(text(
                "SELECT count(*) FROM profiles p WHERE p.merged_into_id IS NULL "
                "AND NOT p.is_deleted AND NOT EXISTS "
                "(SELECT 1 FROM profile_traits t WHERE t.profile_id=p.id)"))
        # Assert the real queued jobs did the work BEFORE any accuracy helper.
        # Never repair missing traits/history here: that would mask a pipeline failure.
        if not any(j["type"] == "traits.recompute" and j["status"] == "succeeded"
                   for j in report["jobs"]):
            raise RuntimeError("Normal pipeline did not produce a succeeded trait job")
        if (report["missing_active_traits"] or report["unresolved"]
                or report["counts"]["trait_dirty_profiles"]
                or not report["counts"]["trait_snapshots"]
                or not report["counts"]["dashboard_daily"]
                or not report["counts"]["dashboard_kpis"]
                or (report["counts"]["events"] and not report["counts"]["dashboard_event_daily"])):
            raise RuntimeError("Normal pipeline left unresolved/dirty/missing trait or history work")
        for job in report["jobs"]:
            job["duration_seconds"] = (
                (job["finished_at"] - job["started_at"]).total_seconds()
                if job["started_at"] and job["finished_at"] else None)
        env = dict(os.environ, TMPDIR=str(output))
        with (output / "identity-accuracy.txt").open("w") as accuracy:
            subprocess.run(
                [sys.executable, str(Path(__file__).with_name("identity_acceptance.py")),
                 "--ground-truth", str(directory / "ground_truth.json")],
                cwd=BACKEND, env=env, stdout=accuracy, stderr=subprocess.STDOUT, check=True)
        if check.returncode:
            raise RuntimeError("Final real load-status did not pass")
        report["completed"] = True
        report["accuracy_requires_review"] = True
    finally:
        report["wall_seconds"] = time.perf_counter() - started
        write_json(output / "result.json", report)


PG17 = Path("/nix/store/jawi15brxq4h3a9snmlr9szx0zcaf2yx-postgresql-17.5/bin")


def controller(args):
    """Foreground supervisor; parent launches this with its persistent task API."""
    output = safe_directory(args.output)
    data = output / "postgres"
    if data.exists() or (output / "private-env.json").exists():
        raise ValueError("Use a new controller output directory; retained evidence is not overwritten")
    if args.greenlight_file and args.greenlight_file.exists():
        raise ValueError("Greenlight file must be absent at controller startup")
    socket = Path(tempfile.mkdtemp(prefix="kinship-bench-"))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("PG") and key != "DATABASE_URL"}
    env.update(APP_ENV="test", AUTH_MODE="dev", SECRET_KEY=secrets.token_hex(32),
               PII_HASH_PEPPER=secrets.token_hex(32),
               FERNET_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
               KINSHIP_BENCHMARK_ISOLATED_TESTS="1", WORKER_CONCURRENCY=str(args.workers),
               WORKER_STOP_GRACE_SECONDS="20", SEED_LOAD_TIMEOUT_SECONDS=str(args.timeout),
               GMAIL_STYLE_DOMAINS="gmail.com,googlemail.com,gmail.test",
               PYTHONPATH=str(BACKEND), TMPDIR=str(output),
               PATH=str(PG17) + os.pathsep + os.environ["PATH"])
    write_json(output / "resources.json", resources())
    worker = None
    child = None
    server_started = False

    def stop_requested(signum, frame):
        raise KeyboardInterrupt(f"Controller received signal {signum}")

    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    try:
        subprocess.run([str(PG17 / "initdb"), "-D", str(data), "-U", "benchmark",
                        "-A", "trust", "--no-locale", "--encoding=UTF8"], check=True)
        if (data / "PG_VERSION").read_text().strip() != "17":
            raise RuntimeError("Expected private PostgreSQL major 17")
        subprocess.run([
            str(PG17 / "pg_ctl"), "-D", str(data), "-l", str(output / "postgres.log"),
            "-o", f"-k {socket} -h '' -p 5432 -c shared_buffers=256MB "
                  "-c work_mem=8MB -c max_connections=40 -c max_wal_size=1GB",
            "-w", "start"], check=True)
        server_started = True
        write_json(output / "private-ready.json", {
            "socket": str(socket), "data": str(data), "pg_bin": str(PG17),
            "state": ("private PG ready; explicitly approved at launch"
                      if args.start_approved else
                      "private PG ready; waiting for parent greenlight; no imports started"),
            "greenlight_file": (str(args.greenlight_file.resolve())
                               if args.greenlight_file else None),
            "start_approved": args.start_approved,
        })
        if not args.start_approved:
            print("PRIVATE PG READY; waiting for parent greenlight file", flush=True)
            while not args.greenlight_file.exists():
                time.sleep(2)
            if args.greenlight_file.read_text().strip() != "STABLE_CODE_APPROVED":
                raise ValueError("Greenlight file must contain STABLE_CODE_APPROVED")
        # No worker or importer runs before this explicit approval.
        for label, fixture in (("preflight", args.preflight_directory),
                               ("250k", args.directory)):
            fixture = fixture.resolve()
            verify(fixture)
            database = "full_load_" + label
            run_output = safe_directory(output / label)
            env.update(DATABASE_URL=f"postgresql+psycopg://benchmark@/{database}?host={socket}",
                       UPLOAD_DIR=str(run_output / "uploads"),
                       EXPORT_DIR=str(run_output / "exports"))
            private_env = {key: env[key] for key in (
                "DATABASE_URL", "APP_ENV", "AUTH_MODE", "SECRET_KEY", "PII_HASH_PEPPER",
                "FERNET_KEY", "KINSHIP_BENCHMARK_ISOLATED_TESTS", "UPLOAD_DIR",
                "EXPORT_DIR", "GMAIL_STYLE_DOMAINS")}
            write_json(run_output / "private-env.json", private_env)
            (run_output / "private-env.json").chmod(0o600)
            subprocess.run([str(PG17 / "createdb"), "-h", str(socket), "-U",
                            "benchmark", database], env=env, check=True)
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                           cwd=BACKEND, env=env, check=True)
            with (run_output / "worker.log").open("w") as worker_log, (
                    run_output / "pipeline.log").open("w") as pipeline_log:
                worker = subprocess.Popen([sys.executable, "-m", "app.worker"],
                                          cwd=BACKEND, env=env, stdout=worker_log,
                                          stderr=subprocess.STDOUT, start_new_session=True)
                command = [sys.executable, str(Path(__file__).resolve()), "run",
                           "--directory", str(fixture), "--output", str(run_output),
                           "--parent-greenlight"]
                if label == "250k":
                    command += ["--preflight-report", str(output / "preflight" / "result.json")]
                child = subprocess.Popen(command, cwd=BACKEND, env=env,
                                         stdout=pipeline_log, stderr=subprocess.STDOUT)
                started = time.monotonic()
                while child.poll() is None:
                    if worker.poll() is not None:
                        raise RuntimeError("Private worker supervisor exited during acceptance")
                    if time.monotonic() - started > args.timeout:
                        raise TimeoutError("Private acceptance deadline exceeded")
                    time.sleep(2)
                if child.returncode:
                    raise RuntimeError(f"{label} pipeline failed; evidence preserved")
                worker.terminate()
                worker.wait(timeout=40)
                worker = None
                child = None
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if worker is not None:
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    pass
            # Also reap spawned worker processes if the supervisor failed.
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.wait()
        if server_started:
            subprocess.run([str(PG17 / "pg_ctl"), "-D", str(data), "-m", "fast",
                            "-w", "stop"], check=True)
        print("Private server stopped; fixture, PGDATA and evidence retained", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--directory", type=Path, required=True)
    prep.add_argument("--people", type=int, default=250_000)
    load = sub.add_parser("run")
    load.add_argument("--directory", type=Path, required=True)
    load.add_argument("--output", type=Path, required=True)
    load.add_argument("--parent-greenlight", action="store_true")
    load.add_argument("--preflight-report", type=Path)
    control = sub.add_parser("controller")
    control.add_argument("--directory", type=Path, required=True)
    control.add_argument("--preflight-directory", type=Path, required=True)
    control.add_argument("--output", type=Path, required=True)
    approval = control.add_mutually_exclusive_group(required=True)
    approval.add_argument("--greenlight-file", type=Path)
    approval.add_argument("--start-approved", action="store_true",
                          help="Explicit parent stable-code approval; start without waiting")
    control.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    control.add_argument("--timeout", type=int, default=14400)
    args = parser.parse_args()
    {"prepare": prepare, "run": run, "controller": controller}[args.command](args)


if __name__ == "__main__":
    main()
