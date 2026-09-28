#!/usr/bin/env python3
"""Restart/reuse a retained PRIVATE benchmark cluster in a persistent terminal.

Configuration comes solely from the fixture's private-env.json. Never supply an
application data directory. Serial use only: this starts/stops the private server.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--clone-from")
    parser.add_argument("--snapshot")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--stage", choices=("resolve", "incremental", "traits-prepare", "traits-measure"), required=True)
    parser.add_argument("--profiles", type=int, default=250000)
    parser.add_argument("--unprofiled", action="store_true")
    args = parser.parse_args()
    for name in (args.database, args.clone_from, args.snapshot):
        if name and not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("Use a simple lowercase benchmark database name")
    root = args.cluster.resolve()
    fixture = json.loads((root / "private-env.json").read_text())
    if fixture.get("APP_ENV") != "test" or fixture.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        raise RuntimeError("Not an isolated benchmark directory")
    original_url = fixture["DATABASE_URL"]
    socket = original_url.split("?host=")[1]
    if not socket.startswith("/tmp/kinship-bench-") or not original_url.startswith("postgresql+psycopg://benchmark@/"):
        raise RuntimeError("Not a private benchmark URL")
    Path(socket).mkdir(mode=0o700, exist_ok=True)
    env = {**os.environ, **fixture}
    env["DATABASE_URL"] = f"postgresql+psycopg://benchmark@/{args.database}?host={socket}"
    args.output.mkdir(parents=True, exist_ok=True)
    sql = ["psql", "-h", socket, "-U", "benchmark", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c"]
    subprocess.run(["pg_ctl", "-D", str(root / "postgres"), "-l", str(root / "postgres.log"),
                    "-o", f"-k {socket} -h '' -p 5432 -c shared_preload_libraries=pg_stat_statements "
                    "-c shared_buffers=512MB -c work_mem=32MB", "-w", "start"], check=True)
    scripts = Path(__file__).resolve().parent
    common = ["--seed-dir", str(args.seed_dir.resolve()), "--output", str(args.output.resolve())]
    try:
        if args.clone_from:
            subprocess.run(sql + [f"CREATE DATABASE {args.database} WITH TEMPLATE {args.clone_from} STRATEGY WAL_LOG"], check=True)
        if args.stage == "incremental":
            subprocess.run([sys.executable, str(scripts / "identity_followup_benchmark.py"),
                            "incremental-import"] + common, env=env, check=True)
        if args.stage in ("resolve", "incremental"):
            # Target is evaluated using actual measured duration. Avoid calling a
            # complete atomic bulk pass incomplete just because it exceeded target.
            subprocess.run([sys.executable, str(scripts / "identity_benchmark.py"),
                            "--phase", "resolve", "--seconds", "3600"] + common
                           + (["--unprofiled"] if args.unprofiled else []), env=env, check=True)
        else:
            subprocess.run([sys.executable, str(scripts / "identity_followup_benchmark.py"),
                            args.stage, "--profiles", str(args.profiles)] + common, env=env, check=True)
        if args.snapshot:
            subprocess.run(sql + [f"CREATE DATABASE {args.snapshot} WITH TEMPLATE {args.database} STRATEGY WAL_LOG"], check=True)
    finally:
        subprocess.run(["pg_ctl", "-D", str(root / "postgres"), "-m", "fast", "-w", "stop"], check=True)


if __name__ == "__main__":
    main()