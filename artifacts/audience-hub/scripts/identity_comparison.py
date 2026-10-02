#!/usr/bin/env python3
"""Counterbalanced, unprofiled committed-resolver comparison in private PostgreSQL.

Prepare once, then run only after other heavy tests have stopped. Only one clone
exists at a time. The application DATABASE_URL is never used.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

SCRIPTS = Path(__file__).resolve().parent
BACKEND = SCRIPTS.parent / "backend"
COMMITS = ("ecb45a4", "3b7e830")
ORDER = (COMMITS[0], COMMITS[1], COMMITS[1], COMMITS[0], COMMITS[0], COMMITS[1])


def child(module_path: Path, output: Path):
    sys.path.insert(0, str(BACKEND))
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.db import engine
    if (os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1"
            or os.environ.get("APP_ENV") != "test"
            or engine.url.database != "comparison_run"
            or not str(engine.url.query.get("host", "")).startswith("/tmp/kinship-bench-")):
        raise RuntimeError("Private comparison database required")
    spec = importlib.util.spec_from_file_location("app.identity.bulk", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with Session(engine) as db:
        db.execute(text("SELECT pg_stat_statements_reset()"))
        db.commit()
        start = time.perf_counter()
        result = dict(records=0, profiles_created=0, merges=0, calls=0)
        while True:
            group = module.resolve_bulk(db)
            db.commit()
            result["calls"] += 1
            for key in ("records", "profiles_created", "merges"):
                result[key] += group[key]
            if not group["records"]:
                break
        result["seconds"] = time.perf_counter() - start
        result["module_sha256"] = hashlib.sha256(module_path.read_bytes()).hexdigest()
        result["profiled"] = False
        result["sql_top10"] = [dict(row) for row in db.execute(text("""
            SELECT query, calls, total_exec_time, shared_blks_hit,
              shared_blks_read, temp_blks_written
            FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database
              WHERE datname=current_database())
            ORDER BY total_exec_time DESC LIMIT 10
        """)).mappings()]
    engine.dispose()
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "sql_top10"}), flush=True)


def prepare(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    seed = root / "seed"
    if not (seed / "generator_stats.json").exists():
        subprocess.run([sys.executable, "-c",
            "from app.seed import generate_seed; from pathlib import Path; "
            f"generate_seed(profiles=250000, scale='medium', random_seed=20250308, output_dir=Path({str(seed)!r}))"],
            cwd=BACKEND, check=True)
    for commit in COMMITS:
        code = subprocess.check_output(["git", "show",
            f"{commit}:artifacts/audience-hub/backend/app/identity/bulk.py"])
        (root / f"bulk-{commit}.py").write_bytes(code)
    subprocess.run([sys.executable, str(SCRIPTS / "identity_benchmark.py"),
        "--output", str(root / "cluster"), "--seed-dir", str(seed),
        "--load-only"], check=True)
    print("Prepared private 250k contact fixture; server stopped. Await timing approval.", flush=True)


def run(root: Path):
    fixture = json.loads((root / "cluster/private-env.json").read_text())
    if fixture.get("APP_ENV") != "test" or fixture.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        raise RuntimeError("Not a private fixture")
    socket = fixture["DATABASE_URL"].split("?host=")[1]
    if not socket.startswith("/tmp/kinship-bench-"):
        raise RuntimeError("Not a private socket")
    Path(socket).mkdir(exist_ok=True)
    env = {**os.environ, **fixture}
    env["DATABASE_URL"] = f"postgresql+psycopg://benchmark@/comparison_run?host={socket}"
    data = root / "cluster/postgres"
    sql = ["psql", "-h", socket, "-U", "benchmark", "-d", "postgres",
           "-v", "ON_ERROR_STOP=1", "-c"]
    subprocess.run(["pg_ctl", "-D", str(data), "-l", str(root / "cluster/postgres.log"),
        "-o", f"-k {socket} -h '' -p 5432 -c shared_preload_libraries=pg_stat_statements "
              "-c shared_buffers=512MB -c work_mem=32MB", "-w", "start"], check=True)
    results = []
    try:
        for number, commit in enumerate(ORDER, 1):
            out = root / f"run-{number}-{commit}.json"
            if out.exists():
                results.append({"commit": commit, **json.loads(out.read_text())})
                continue
            subprocess.run(sql + ["DROP DATABASE IF EXISTS comparison_run"], check=True)
            subprocess.run(sql + ["CREATE DATABASE comparison_run WITH TEMPLATE benchmark STRATEGY WAL_LOG"], check=True)
            try:
                subprocess.run([sys.executable, str(Path(__file__).resolve()),
                    "--root", str(root), "--phase", "child", "--module", str(root / f"bulk-{commit}.py"),
                    "--result", str(out)], env=env, check=True)
                results.append({"commit": commit, **json.loads(out.read_text())})
            finally:
                subprocess.run(sql + ["DROP DATABASE comparison_run"], check=True)
            report = {
                "requested_people": 250000, "fixture": "real medium seed, contacts only",
                "random_seed": 20250308, "order": ORDER, "runs": results,
                "medians": {c: statistics.median(r["seconds"] for r in results if r["commit"] == c)
                            for c in COMMITS if any(r["commit"] == c for r in results)},
                "complete": len(results) == 6,
            }
            (root / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop"], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--phase", choices=("prepare", "run", "child"), required=True)
    parser.add_argument("--module", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if os.environ.get("APP_ENV") == "production":
        raise RuntimeError("This local private-cluster comparison is not a production command")
    if args.phase == "child":
        child(args.module.resolve(), args.result.resolve())
    elif args.phase == "prepare":
        prepare(args.root.resolve())
    else:
        run(args.root.resolve())