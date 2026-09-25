#!/usr/bin/env python3
"""Capture real profile HTTP responses from a disposable medium-seed database.

Never reads DATABASE_URL. Default medium population is 50,000; --profiles is an
explicit smaller smoke population, recorded in the evidence (not called full medium).
"""
import argparse
import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def capture(args):
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from fastapi.testclient import TestClient
    from app.db import engine
    from app.seed import generate_seed
    from app.importer import SEED_SOURCES, _mapping
    from app.imports.service import run_import
    from app.identity.resolver import resolve_batch
    from app.traits.engine import recompute_traits
    from app.auth.deps import current_user
    from app.main import app
    from app.models import User

    started = time.monotonic()
    seed_dir = Path(os.environ["UPLOAD_DIR"]) / "seed"
    def budget():
        if args.budget and time.monotonic() - started >= args.budget:
            with Session(engine) as progress:
                print("Unresolved source records:", progress.execute(text(
                    "SELECT count(*) FROM source_records WHERE profile_id IS NULL")).scalar_one(), flush=True)
            print("Capture paused at committed checkpoint; rerun with same --work-dir", flush=True)
            raise SystemExit(75)
    if (seed_dir / "generator_stats.json").is_file():
        generated = {"files": {name: seed_dir / name for name in SEED_SOURCES}}
    else:
        generated = generate_seed(profiles=args.profiles, scale="medium", output_dir=seed_dir)
    imports = []
    for filename, path in generated["files"].items():
        key, name, kind = SEED_SOURCES[filename]
        with path.open() as stream:
            reader = csv.reader(stream)
            headers = next(reader)
            count = sum(1 for _ in reader)
        with Session(engine) as db:
            previous = db.execute(text("""SELECT filename,status,rows_total,rows_ok,rows_rejected
                FROM imports WHERE filename=:name AND status='completed'"""),
                {"name": filename}).mappings().first()
            if previous:
                imports.append(dict(previous))
                continue
            sid = db.execute(text("""INSERT INTO sources
                (key,name,kind,record_types,priority,is_active)
                VALUES (:key,:name,'csv',:types,50,true) RETURNING id"""),
                dict(key=key, name=name, types=[kind])).scalar_one()
            iid = db.execute(text("""INSERT INTO imports
                (source_id,filename,file_path,file_sha256,record_type,mapping,
                 status,rows_total,rows_ok,rows_rejected)
                VALUES (:sid,:filename,:path,:sha,:kind,CAST(:mapping AS jsonb),
                        'running',:count,0,0) RETURNING id"""),
                dict(sid=sid, filename=filename, path=str(path),
                     sha=hashlib.sha256(path.read_bytes()).hexdigest(), kind=kind,
                     mapping=json.dumps(_mapping(headers, kind)), count=count)).scalar_one()
            db.commit()
        run_import(iid)
        with Session(engine) as db:
            row = dict(db.execute(text(
                "SELECT filename,status,rows_total,rows_ok,rows_rejected FROM imports WHERE id=:id"),
                {"id": iid}).mappings().one())
            assert row["status"] == "completed", row
            imports.append(row)
        print(json.dumps(row), flush=True)
        budget()
    while True:
        with Session(engine) as db:
            result = resolve_batch(db)
            db.commit()
        if not result["records"]:
            break
        budget()
    with Session(engine) as db:
        users = {}
        for role in ("admin", "viewer"):
            user = User(subject=f"capture:{role}", email=f"{role}@example.test",
                        name=role, role=role, is_active=True)
            db.add(user)
            db.flush()
            users[role] = user
        db.commit()
        for user in users.values():
            db.refresh(user)
            db.expunge(user)
        counts = {table: db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                  for table in ("profiles", "source_records", "gifts", "events", "enrichment_values")}
    client = TestClient(app)
    responses = []

    def get(label, pid, role):
        app.dependency_overrides[current_user] = lambda: users[role]
        response = client.get(f"/api/profiles/{pid}", follow_redirects=False)
        assert response.status_code == (301 if label == "merged" else 200), response.text
        responses.append(dict(label=label, role=role, id=pid,
                              status=response.status_code, body=response.json()))

    # Capture missing traits through the actual SQL LEFT JOIN, before recomputation.
    for role in users:
        get("no-traits-row", 1, role)
    with Session(engine) as db:
        recompute_traits(db, as_of=__import__("datetime").date(2026, 3, 1))
        db.commit()
    for role in users:
        for pid in range(1, 51):
            get(f"profile-{pid}", pid, role)
    with Session(engine) as db:
        catalog = client.get("/api/traits").json()
        # Edge mutations affect only the disposable database, never fabricated JSON.
        # Each is captured and rolled back before the next scenario.
        cases = {
            "prospect": "UPDATE profile_traits SET donor_status='prospect', gift_count_total=0, ltv_total=0 WHERE profile_id=1; DELETE FROM gifts WHERE profile_id=1",
            "lapsed": "UPDATE profile_traits SET donor_status='lapsed' WHERE profile_id=1",
            "no-email": "UPDATE profiles SET email=NULL WHERE id=1; DELETE FROM identifiers WHERE profile_id=1 AND type='email'",
            "no-phone": "UPDATE profiles SET phone=NULL WHERE id=1; DELETE FROM identifiers WHERE profile_id=1 AND type='phone'",
            "no-consents": "DELETE FROM consents WHERE profile_id=1",
            "no-enrichment": "DELETE FROM enrichment_values WHERE profile_id=1",
            "expired-enrichment": "UPDATE enrichment_values SET license_expires_at='2000-01-01' WHERE profile_id=1",
            "merged": "UPDATE profiles SET merged_into_id=2 WHERE id=1",
            "events-no-gifts": "DELETE FROM gifts WHERE profile_id=1",
            "five9-only": "DELETE FROM gifts WHERE profile_id=1; DELETE FROM source_records WHERE profile_id=1 AND source_id NOT IN (SELECT id FROM sources WHERE key='five9')",
        }
        from app.db import session_scope
        # HTTP route commits audit rows; use a savepoint-bound session so changes
        # are visible to the route without committing the outer test transaction.
        for label, sql in cases.items():
            with engine.connect() as connection:
                transaction = connection.begin()
                edge_db = Session(bind=connection, join_transaction_mode="create_savepoint")
                pid = 1
                if label in ("events-no-gifts", "five9-only"):
                    pid = edge_db.execute(text("""SELECT e.profile_id FROM events e
                        JOIN sources s ON s.id=e.source_id JOIN profiles p ON p.id=e.profile_id
                        WHERE s.key='five9' AND p.merged_into_id IS NULL
                        ORDER BY e.profile_id LIMIT 1""")).scalar_one()
                    sql = sql.replace("profile_id=1", f"profile_id={pid}")
                for statement in sql.split(";"):
                    edge_db.execute(text(statement))
                edge_db.commit()
                def edge_session():
                    yield edge_db
                app.dependency_overrides[session_scope] = edge_session
                try:
                    for role in users:
                        get(label, pid, role)
                finally:
                    app.dependency_overrides.pop(session_scope)
                    edge_db.close()
                    transaction.rollback()
    evidence = dict(schema_version=1, generator="app.seed.generate_seed",
                    scale="medium", requested_profiles=args.profiles,
                    full_medium=args.profiles == 50_000, random_seed=20250308,
                    pipeline=["run_import", "resolve_batch", "recompute_traits", "FastAPI TestClient"],
                    counts=counts, imports=imports, catalog=catalog, responses=responses,
                    api_source_sha256=hashlib.sha256(
                        (ROOT / "backend/app/profiles/api.py").read_bytes()).hexdigest(),
                    seed_csv_sha256={name: hashlib.sha256(path.read_bytes()).hexdigest()
                                     for name, path in generated["files"].items()},
                    final_process_elapsed_seconds=round(time.monotonic() - started, 2))
    Path(args.output).write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({key: evidence[key] for key in
                      ("full_medium", "requested_profiles", "counts", "final_process_elapsed_seconds")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=int, default=50_000)
    parser.add_argument("--output", default=str(ROOT / "scripts/fixtures/profiles-medium.json"))
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work-dir", help="Retain isolated cluster for resumable, budgeted runs")
    parser.add_argument("--budget", type=int, default=0, help="Pause child after this many seconds (exit 75)")
    args = parser.parse_args()
    if args.profiles < 50:
        parser.error("At least 50 generated people are required")
    if args.budget and not args.work_dir and not args.child:
        parser.error("--budget requires --work-dir to preserve the checkpoint")
    if args.child:
        if os.environ.get("PROFILE_CAPTURE_ISOLATED") != "1":
            raise RuntimeError("Child requires isolated-cluster launcher")
        return capture(args)
    if os.environ.get("APP_ENV") == "production":
        raise RuntimeError("Forbidden in production")
    Path(args.output).resolve().parent.mkdir(parents=True, exist_ok=True)
    from contextlib import nullcontext
    context = nullcontext(args.work_dir) if args.work_dir else tempfile.TemporaryDirectory(prefix="profile-capture-")
    with context as directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        data, socket = root / "pg", root / "socket"
        socket.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        env.update(APP_ENV="test", AUTH_MODE="dev", PROFILE_CAPTURE_ISOLATED="1",
                   SECRET_KEY=secrets.token_hex(32), PII_HASH_PEPPER=secrets.token_hex(32),
                   FERNET_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                   DATABASE_URL=f"postgresql+psycopg://capture@/capture?host={socket}",
                   UPLOAD_DIR=str(root / "uploads"), EXPORT_DIR=str(root / "exports"),
                   PYTHONPATH=str(ROOT / "backend"))
        config_path = root / "capture-environment.json"
        if config_path.exists():
            saved = json.loads(config_path.read_text())
            assert saved["profiles"] == args.profiles, "Cannot change population during resume"
            env.update(saved["environment"])
        else:
            persisted = {key: env[key] for key in ("DATABASE_URL", "SECRET_KEY", "PII_HASH_PEPPER", "FERNET_KEY")}
            config_path.touch(mode=0o600)
            config_path.write_text(json.dumps({"profiles": args.profiles, "environment": persisted}))
        def run(command):
            subprocess.run(command, env=env, cwd=ROOT / "backend", check=True)
        fresh = not (data / "PG_VERSION").exists()
        if fresh:
            run(["initdb", "-D", str(data), "-U", "capture", "-A", "trust", "--no-locale", "--encoding=UTF8"])
        run(["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
             "-o", f"-k {socket} -h ''", "-w", "start"])
        try:
            if fresh:
                run(["createdb", "-h", str(socket), "-U", "capture", "capture"])
            run([sys.executable, "-m", "alembic", "upgrade", "head"])
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child",
                 "--profiles", str(args.profiles), "--budget", str(args.budget),
                 "--output", str(Path(args.output).resolve())], env=env, cwd=ROOT / "backend")
            if result.returncode:
                raise SystemExit(result.returncode)
        finally:
            run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"])


if __name__ == "__main__":
    main()