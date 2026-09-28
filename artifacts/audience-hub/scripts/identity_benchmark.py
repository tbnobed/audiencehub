#!/usr/bin/env python3
"""Private, contacts-only before/after benchmark; never uses application DB URLs."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

BACKEND = Path(__file__).resolve().parents[1] / "backend"
FILES = ("donor_crm_contacts.csv", "esp_contacts.csv", "zeta_enrichment.csv")


def child(args):
    sys.path.insert(0, str(BACKEND))
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.db import engine
    if (os.environ.get("APP_ENV") != "test"
            or os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1"
            or engine.url.host
            or not str(engine.url.query.get("host", "")).startswith("/tmp/kinship-bench-")):
        raise RuntimeError("Private benchmark socket required")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.phase == "load":
        from app.importer import _mapping, SEED_SOURCES
        from app.imports.service import csv_reader, run_import
        start = time.perf_counter()
        if args.small:
            from app.seed import generate_seed
            generate_seed(profiles=args.small, scale="small", random_seed=20250308,
                          output_dir=args.seed_dir)
        imports = []
        for filename in FILES:
            path = (args.seed_dir / filename).resolve()
            headers, rows = csv_reader(path)
            try:
                count = sum(1 for _ in rows)
            finally:
                rows.close()
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            key, name, kind = SEED_SOURCES[filename]
            with Session(engine) as db:
                sid = db.execute(text("SELECT id FROM sources WHERE key=:key"), {"key": key}).scalar()
                if sid is None:
                    sid = db.execute(text("""
                        INSERT INTO sources(key,name,kind,record_types,priority,is_active)
                        VALUES (:key,:name,'csv',:types,50,true) RETURNING id
                    """), dict(key=key, name=name, types=[kind])).scalar_one()
                iid = db.execute(text("""
                    INSERT INTO imports(source_id,filename,file_path,file_sha256,record_type,
                    mapping,status,rows_total,rows_ok,rows_rejected)
                    VALUES (:sid,:filename,:path,:digest,:kind,CAST(:mapping AS jsonb),
                    'running',:count,0,0) RETURNING id
                """), dict(sid=sid, filename=filename, path=str(path), digest=digest,
                           kind=kind, mapping=json.dumps(_mapping(headers, kind)), count=count)).scalar_one()
                db.commit()
            before = time.perf_counter()
            run_import(iid)
            with Session(engine) as db:
                item = dict(db.execute(text("SELECT filename,status,rows_total,rows_ok,rows_rejected "
                                            "FROM imports WHERE id=:id"), {"id": iid}).mappings().one())
                if item["status"] != "completed":
                    raise RuntimeError(f"Import {iid} did not complete")
                item["seconds"] = time.perf_counter() - before
                imports.append(item)
                print(json.dumps(item), flush=True)
                (output / "imports.json").write_text(json.dumps(imports, indent=2))
        with Session(engine) as db:
            db.execute(text("UPDATE jobs SET status='cancelled' WHERE status='queued'"))
            db.execute(text("ANALYZE"))
            db.commit()
            counts = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                      for t in ("source_records", "gifts", "events", "profiles")}
        (output / "load.json").write_text(json.dumps(
            dict(seconds=time.perf_counter() - start, counts=counts), indent=2))
    else:
        if args.frozen:
            for module in ("survivorship", "resolver"):
                name = f"app.identity.{module}"
                spec = importlib.util.spec_from_file_location(name, args.frozen / f"{module}.py")
                loaded = importlib.util.module_from_spec(spec)
                sys.modules[name] = loaded
                spec.loader.exec_module(loaded)
        if args.frozen:
            from app.identity.resolver import resolve_batch as resolver
        else:
            from app.identity.bulk import resolve_bulk as resolver
        module_file = Path(sys.modules[resolver.__module__].__file__)
        (output / "engine-version.json").write_text(json.dumps({
            "module": resolver.__module__,
            "sha256": hashlib.sha256(module_file.read_bytes()).hexdigest(),
            "profiled": not args.unprofiled,
        }, indent=2))
        from app.identity.profiling import profile_resolution
        with Session(engine) as db:
            if args.unprofiled:
                started = time.perf_counter()
                result = dict(complete=False, profiled=False, committed_groups=0,
                              records=0, profiles_created=0, merges=0)
                while True:
                    group = resolver(db)
                    db.commit()
                    result["committed_groups"] += 1
                    for key in ("records", "profiles_created", "merges"):
                        result[key] += group[key]
                    if group["records"] == 0:
                        result["complete"] = True
                        break
                    if time.perf_counter() - started >= args.seconds:
                        break
                result["seconds"] = time.perf_counter() - started
                (output / "resolver-timing.json").write_text(json.dumps(result, indent=2))
            else:
                result = profile_resolution(db, output, resolver=resolver, max_seconds=args.seconds)
            print(json.dumps(result), flush=True)
        if args.small:
            with (output / "accuracy.txt").open("w") as stream:
                subprocess.run([sys.executable, str(Path(__file__).with_name("identity_acceptance.py")),
                                "--ground-truth", str(args.seed_dir / "ground_truth.json")],
                               stdout=stream, check=True)
    engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--frozen", type=Path)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--small", type=int, default=0)
    parser.add_argument("--unprofiled", action="store_true",
                        help="Wall-clock acceptance timing without cProfile/SQL statistics reset")
    parser.add_argument("--load-only", action="store_true",
                        help="Stop after real imports; split long runs across terminal deadlines")
    parser.add_argument("--phase", choices=["load", "resolve"])
    args = parser.parse_args()
    if args.phase:
        child(args)
        return
    if os.environ.get("APP_ENV") == "production":
        raise RuntimeError("Benchmark forbidden in production")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    data = output / "postgres"
    socket = Path(tempfile.mkdtemp(prefix="kinship-bench-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update(APP_ENV="test", AUTH_MODE="dev", SECRET_KEY=secrets.token_hex(32),
               PII_HASH_PEPPER=secrets.token_hex(32),
               FERNET_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
               KINSHIP_BENCHMARK_ISOLATED_TESTS="1",
               UPLOAD_DIR=str(output / "uploads"), EXPORT_DIR=str(output / "exports"),
               DATABASE_URL=f"postgresql+psycopg://benchmark@/benchmark?host={socket}")
    # Private fixture-only credentials retained so the parent can clone/reuse the DB.
    private_env = {key: env[key] for key in (
        "APP_ENV", "AUTH_MODE", "SECRET_KEY", "PII_HASH_PEPPER", "FERNET_KEY",
        "KINSHIP_BENCHMARK_ISOLATED_TESTS", "UPLOAD_DIR", "EXPORT_DIR", "DATABASE_URL")}
    (output / "private-env.json").write_text(json.dumps(private_env))
    (output / "private-env.json").chmod(0o600)
    subprocess.run(["initdb", "-D", str(data), "-U", "benchmark", "-A", "trust",
                    "--no-locale", "--encoding=UTF8"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["pg_ctl", "-D", str(data), "-l", str(output / "postgres.log"),
                    "-o", f"-k {socket} -h '' -p 5432 -c shared_preload_libraries=pg_stat_statements "
                    "-c shared_buffers=512MB -c work_mem=32MB", "-w", "start"],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["createdb", "-h", str(socket), "-U", "benchmark", "benchmark"], check=True)
    subprocess.run(["psql", "-h", str(socket), "-U", "benchmark", "-d", "benchmark",
                    "-c", "CREATE EXTENSION pg_stat_statements"], check=True)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=BACKEND, env=env, check=True)
    common = [sys.executable, str(Path(__file__).resolve()), "--output", str(output),
              "--seed-dir", str(args.seed_dir.resolve()), "--seconds", str(args.seconds),
              "--small", str(args.small)]
    subprocess.run(common + ["--phase", "load"], env=env, check=True)
    if args.load_only:
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop"], check=True)
        return
    # Unresolved template gives identical starting data for the after measurement.
    subprocess.run(["psql", "-h", str(socket), "-U", "benchmark", "-d", "postgres",
                    "-c", "CREATE DATABASE unresolved_template WITH TEMPLATE benchmark STRATEGY WAL_LOG"],
                   check=True)
    if args.frozen:
        common += ["--frozen", str(args.frozen.resolve())]
    if args.unprofiled:
        common += ["--unprofiled"]
    subprocess.run(common + ["--phase", "resolve"], env=env, check=True)
    print(f"Retained private PostgreSQL for after comparison: {output}", flush=True)
    print(f"Stop explicitly with: pg_ctl -D {data} -m fast stop", flush=True)


if __name__ == "__main__":
    main()