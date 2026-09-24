"""Synthetic import-only measurements in a new disposable local PostgreSQL cluster.

Never uses DATABASE_URL or application credentials. Inputs and evidence are retained;
the private PostgreSQL process is stopped on exit. Identity/trait workers are excluded.
"""
from __future__ import annotations

import argparse
import cProfile
import csv
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import pstats
import secrets
import shutil
import subprocess
import sys
import tempfile
import time


def generate_csv(path: Path, kind: str, count: int) -> list[str]:
    headers = (["external_id", "email", "amount", "gift_date", "fund"] if kind == "gift"
               else ["external_id", "email", "first_name", "last_name", "phone"])
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        for index in range(count):
            email = f"person{index}@example.org"
            writer.writerow(
                [f"gift-{index}", email, "25.50", "2025-01-15", "General"]
                if kind == "gift" else
                [f"contact-{index}", email, "Test", f"Person{index}", "+12025550123"]
            )
    return headers


def acceptance(results: list[dict]) -> dict[str, bool]:
    checks = {}
    for result in results:
        kind = result["kind"]
        checks[f"{kind}_complete"] = (
            result["status"] == "completed" and result["rows_ok"] == result["rows"]
            and result["rows_rejected"] == 0
        )
        checks[f"{kind}_gte_5000_rows_per_second"] = result["rows_per_second"] >= 5000
        checks[f"{kind}_source_count"] = result["source_records"] == result["rows"]
        if kind == "gift":
            checks["gift_database_count"] = result["gifts"] == result["rows"]
            checks["gift_under_300_seconds"] = result["elapsed_seconds"] < 300
    return checks


def _measure(directory: Path, gift_rows: int, contact_rows: int, profile: bool) -> dict:
    # All application imports happen only in the isolated child after configuration.
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.imports.service import run_import

    tasks = []
    for kind, count in (("gift", gift_rows), ("contact", contact_rows)):
        if not count:
            continue
        path = directory / f"{kind}.csv"
        headers = generate_csv(path, kind, count)
        with Session(engine) as db:
            source_id = db.execute(text("""
                INSERT INTO sources (key,name,kind,record_types,priority,is_active)
                VALUES (:key,:key,'csv',:types,10,true) RETURNING id
            """), {"key": f"benchmark_{kind}", "types": [kind]}).scalar_one()
            import_id = db.execute(text("""
                INSERT INTO imports
                (source_id,filename,file_path,file_sha256,record_type,mapping,
                 status,rows_total,rows_ok,rows_rejected)
                VALUES (:source,:filename,:path,:digest,:kind,CAST(:mapping AS jsonb),
                        'running',:count,0,0) RETURNING id
            """), {"source": source_id, "filename": path.name, "path": str(path),
                   "digest": hashlib.sha256(path.read_bytes()).hexdigest(), "kind": kind,
                   "mapping": json.dumps({"columns": {h: h for h in headers}, "options": {}}),
                   "count": count}).scalar_one()
            db.commit()
        tasks.append((kind, count, import_id))

    def execute(task):
        kind, count, import_id = task
        profiler = cProfile.Profile() if profile else None
        start = time.perf_counter()
        if profiler:
            profiler.enable()
        try:
            run_import(import_id)
        finally:
            elapsed = time.perf_counter() - start
            if profiler:
                profiler.disable()
                profiler.dump_stats(str(directory / f"{kind}.prof"))
                with (directory / f"{kind}-top20.txt").open("w") as stream:
                    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats(
                        "cumulative").print_stats(20)
        with Session(engine) as db:
            row = db.execute(text(
                "SELECT status,rows_ok,rows_rejected FROM imports WHERE id=:id"
            ), {"id": import_id}).mappings().one()
            source_count = db.execute(text(
                "SELECT count(*) FROM source_records WHERE last_import_id=:id"
            ), {"id": import_id}).scalar_one()
            gift_count = db.execute(text(
                "SELECT count(*) FROM gifts WHERE source_id="
                "(SELECT source_id FROM imports WHERE id=:id)"
            ), {"id": import_id}).scalar_one() if kind == "gift" else 0
        result = dict(row, kind=kind, rows=count, elapsed_seconds=elapsed,
                      rows_per_second=count / elapsed, source_records=source_count, gifts=gift_count)
        print(json.dumps(result), flush=True)
        return result

    # Match production's process-isolated workers, not GIL-bound Python threads.
    # This local PostgreSQL harness targets Unix hosts (initdb cannot run as root).
    context = multiprocessing.get_context("fork")
    output = context.Queue()

    def child(task):
        engine.dispose(close=False)
        try:
            output.put(("ok", execute(task)))
        except BaseException:
            import traceback
            output.put(("error", traceback.format_exc()))
            raise

    children = [context.Process(target=child, args=(task,)) for task in tasks]
    for process in children:
        process.start()
    results = []
    import queue
    try:
        while len(results) < len(tasks):
            try:
                status, value = output.get(timeout=1)
            except queue.Empty:
                if any(p.exitcode not in (None, 0) for p in children):
                    raise RuntimeError("Benchmark import process exited unexpectedly")
                continue
            if status != "ok":
                raise RuntimeError(value)
            results.append(value)
    finally:
        for process in children:
            if len(results) != len(tasks) and process.is_alive():
                process.terminate()
            process.join()
    report = {"results": results, "checks": acceptance(results),
              "profile_enabled": profile, "python": platform.python_version(),
              "cpu_count": os.cpu_count(),
              "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
              "scope": "CSV import including validation, normalization and database writes; "
                       "excludes CSV generation, migrations, queue wait, identity resolution and traits"}
    (directory / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["checks"], indent=2), flush=True)
    return report


def run_benchmark(args) -> int:
    if not args.imports:
        raise ValueError("Specify --imports to explicitly start an isolated synthetic benchmark.")
    if os.environ.get("APP_ENV", "").lower() == "production":
        raise ValueError("Benchmark is forbidden when APP_ENV=production.")
    gift_rows = args.rows if args.rows is not None else args.gift_rows
    contact_rows = args.rows if args.rows is not None else args.contact_rows
    if gift_rows < 0 or contact_rows < 0 or gift_rows + contact_rows == 0:
        raise ValueError("Row counts must be nonnegative and at least one must be positive.")
    for binary in ("initdb", "pg_ctl", "createdb"):
        if not shutil.which(binary):
            raise RuntimeError(f"Local PostgreSQL binary required: {binary}")
    root = Path(args.output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="imports-", dir=root))
    # Short Unix socket path avoids PostgreSQL's sockaddr_un length limit.
    socket_dir = Path(tempfile.mkdtemp(prefix="kinship-bench-"))
    data = directory / "postgres"
    print(f"Isolated benchmark evidence: {directory}", flush=True)
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("PG"):
            del env[key]
    env.update(APP_ENV="test", AUTH_MODE="dev", SECRET_KEY=secrets.token_hex(32),
               PII_HASH_PEPPER=secrets.token_hex(32), UPLOAD_DIR=str(directory / "uploads"),
               EXPORT_DIR=str(directory / "exports"))
    import base64
    env["FERNET_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    env["DATABASE_URL"] = f"postgresql+psycopg://benchmark@/benchmark?host={socket_dir}"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-U", "benchmark", "-A", "trust",
                        "--no-locale", "--encoding=UTF8"], env=env, check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["pg_ctl", "-D", str(data), "-l", str(directory / "postgres.log"),
                        "-o", f"-k {socket_dir} -h '' -p 5432", "-w", "start"],
                       env=env, check=True, stdout=subprocess.DEVNULL)
        started = True
        subprocess.run(["createdb", "-h", str(socket_dir), "-U", "benchmark", "benchmark"],
                       env=env, check=True)
        # APP_BENCHMARK_BACKEND is deliberately not supported: the imported app determines schema.
        import importlib.util
        backend = Path(importlib.util.find_spec("app").origin).parent.parent
        subprocess.run([sys.executable, "-m", "alembic", "-c", str(backend / "alembic.ini"),
                        "upgrade", "head"], cwd=backend, env=env, check=True)
        if args.test_suite:
            env["AH_WORKER_RECOVERY_TEST_DATABASE_URL"] = env["DATABASE_URL"]
            env["KINSHIP_BENCHMARK_ISOLATED_TESTS"] = "1"
            with (directory / "pytest.txt").open("w") as output:
                test_result = subprocess.run(
                    [sys.executable, "-m", "pytest", "tests", "-q"],
                    cwd=backend, env=env, stdout=output, stderr=subprocess.STDOUT)
            print(f"Isolated test suite exit={test_result.returncode}: {directory / 'pytest.txt'}",
                  flush=True)
            return test_result.returncode
        script = (
            "import runpy; from pathlib import Path; "
            f"m=runpy.run_path({str(Path(__file__).resolve())!r}); "
            f"m['_measure'](Path({str(directory)!r}),{gift_rows},{contact_rows},{args.profile!r})"
        )
        subprocess.run([sys.executable, "-c", script], cwd=backend, env=env, check=True)
        report = json.loads((directory / "results.json").read_text())
        return 0 if all(report["checks"].values()) else 1
    finally:
        if started:
            subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                           env=env, check=True, stdout=subprocess.DEVNULL)
        shutil.rmtree(socket_dir)
        shutil.rmtree(data, ignore_errors=True)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--imports", action="store_true", help="Explicitly run isolated import benchmark.")
    parser.add_argument("--rows", type=int, help="Override both row counts.")
    parser.add_argument("--gift-rows", type=int, default=1_000_000)
    parser.add_argument("--contact-rows", type=int, default=500_000)
    parser.add_argument("--profile", action="store_true", help="Write cProfile files and cumulative top 20.")
    parser.add_argument("--test-suite", action="store_true",
                        help="Run backend pytest suite instead of imports, in the disposable database.")
    parser.add_argument("--output-dir", default=".cache/kinship-benchmarks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    raise SystemExit(run_benchmark(parser.parse_args()))