#!/usr/bin/env python3
"""Opt-in incremental/250k-trait measurements on a CLONE of resolved benchmark data.

Never configure this script with application credentials. Parent runner supplies
its fixture-only environment. CSV preparation does not connect to PostgreSQL.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))


def prepare_incremental(seed: Path, output: Path, count: int):
    """New source, existing real ESP identities: a touched-component benchmark."""
    destination = output / "incremental-esp.csv"
    seen = set()
    with (seed / "esp_contacts.csv").open(newline="") as source, destination.open("w", newline="") as target:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            key = row["external_id"]
            if not key or key in seen:
                continue
            seen.add(key)
            writer.writerow(row)
            if len(seen) == count:
                break
    if len(seen) != count:
        raise ValueError("Not enough unique real ESP rows")
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = dict(records=count, sha256=digest,
                  scenario="new source import of existing identities; not 50k new people",
                  fixture=str(destination))
    (output / "incremental-fixture.json").write_text(json.dumps(result, indent=2))
    return result


def isolated_engine():
    from app.db import engine
    if (os.environ.get("APP_ENV") != "test"
            or os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1"
            or engine.url.host
            or not str(engine.url.query.get("host", "")).startswith("/tmp/kinship-bench-")):
        raise RuntimeError("Only the disposable fixture socket is allowed")
    # Require an explicitly named clone, protecting the before/template databases.
    if not str(engine.url.database).startswith("followup_"):
        raise RuntimeError("Use a clone named followup_incremental or followup_traits")
    return engine


def import_incremental(engine, output: Path):
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.importer import _mapping
    from app.imports.service import run_import
    fixture = json.loads((output / "incremental-fixture.json").read_text())
    path = Path(fixture["fixture"]).resolve()
    with path.open(newline="") as stream:
        headers = next(csv.reader(stream))
    started = time.perf_counter()
    with Session(engine) as db:
        if db.execute(text("SELECT count(*) FROM source_records WHERE resolved_at IS NULL")).scalar_one():
            raise RuntimeError("Resolve the original contact fixture completely first")
        sid = db.execute(text("""
            INSERT INTO sources(key,name,kind,record_types,priority,is_active)
            VALUES ('esp_incremental_benchmark','Incremental ESP benchmark','csv',
                    ARRAY['contact'],50,true) RETURNING id
        """)).scalar_one()
        iid = db.execute(text("""
            INSERT INTO imports(source_id,filename,file_path,file_sha256,record_type,mapping,
                                status,rows_total,rows_ok,rows_rejected)
            VALUES (:sid,:filename,:path,:digest,'contact',CAST(:mapping AS jsonb),
                    'running',:count,0,0) RETURNING id
        """), dict(sid=sid, filename=path.name, path=str(path), digest=fixture["sha256"],
                   count=fixture["records"], mapping=json.dumps(_mapping(headers, "contact")))).scalar_one()
        db.commit()
    run_import(iid)
    with Session(engine) as db:
        report = dict(db.execute(text("SELECT status,rows_total,rows_ok,rows_rejected FROM imports WHERE id=:id"),
                                 {"id": iid}).mappings().one())
        report.update(import_id=iid, seconds=time.perf_counter() - started,
                      unresolved=db.execute(text(
                          "SELECT count(*) FROM source_records WHERE resolved_at IS NULL")).scalar_one())
        db.execute(text("UPDATE jobs SET status='cancelled' WHERE status='queued'"))
        db.commit()
    (output / "incremental-import.json").write_text(json.dumps(report, indent=2))
    if report["status"] != "completed" or report["rows_rejected"] or report["unresolved"] != fixture["records"]:
        raise RuntimeError("Incremental import did not produce exactly the requested pending records")
    return report


def prepare_traits(engine, seed: Path, output: Path, count: int):
    """Typed bulk setup from actual seed CSVs, NOT an importer benchmark."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.imports.validation import _date, _datetime
    started = time.perf_counter()
    report = dict(setup="typed CSV COPY, not production import timing", requested_active_profiles=count,
                  selection="lowest active resolved profile IDs; other profiles marked deleted in clone",
                  timings={}, rejected={}, copied={}, inserted={})
    with Session(engine) as db:
        if db.execute(text("SELECT count(*) FROM source_records WHERE resolved_at IS NULL")).scalar_one():
            raise RuntimeError("Resolve the full 500k contact fixture first")
        if db.execute(text("SELECT count(*) FROM gifts")).scalar_one() or db.execute(text("SELECT count(*) FROM events")).scalar_one():
            raise RuntimeError("Start with a clean contacts-only resolved clone")
        db.execute(text("""
            CREATE TEMP TABLE trait_target_profiles ON COMMIT DROP AS
            SELECT id FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted
            ORDER BY id LIMIT :n
        """), {"n": count})
        db.execute(text("CREATE UNIQUE INDEX ON trait_target_profiles(id)"))
        db.execute(text("ANALYZE trait_target_profiles"))
        if db.execute(text("SELECT count(*) FROM trait_target_profiles")).scalar_one() != count:
            raise RuntimeError("Resolved fixture has fewer active profiles than requested")
        db.execute(text("""
            UPDATE profiles p SET is_deleted=true
            WHERE NOT p.is_deleted
              AND NOT EXISTS(SELECT 1 FROM trait_target_profiles t WHERE t.id=p.id)
        """))
        mapping = dict(db.execute(text("""
            SELECT sr.external_id,sr.profile_id FROM source_records sr
            JOIN sources s ON s.id=sr.source_id JOIN profiles p ON p.id=sr.profile_id
            WHERE s.key='donor_crm' AND p.merged_into_id IS NULL AND NOT p.is_deleted
        """)).all())
        db.execute(text("TRUNCATE profile_traits,trait_snapshots"))
        db.execute(text("UPDATE jobs SET status='cancelled' WHERE status='queued'"))
        db.commit()
        for kind, filename, key in (
            ("gifts", "giving_platform_gifts.csv", "giving_platform"),
            ("events", "five9_calls.csv", "five9"),
        ):
            begin = time.perf_counter()
            sid = db.execute(text("SELECT id FROM sources WHERE key=:key"), {"key": key}).scalar()
            if sid is None:
                sid = db.execute(text("""
                    INSERT INTO sources(key,name,kind,record_types,priority,is_active)
                    VALUES (:key,:key,'csv',:types,50,true) RETURNING id
                """), dict(key=key, types=["gift" if kind == "gifts" else "event"])).scalar_one()
            columns = (
                "source_id,profile_id,external_id,amount,currency,gift_date,fund,campaign,"
                "appeal_code,channel,payment_method,is_recurring,recurring_plan_id"
                if kind == "gifts" else
                "source_id,profile_id,type,name,occurred_at,received_at,message_id,properties,context"
            )
            db.execute(text(f"CREATE TEMP TABLE trait_fixture_stage ON COMMIT DROP AS SELECT {columns} FROM {kind} WHERE false"))
            copied = rejected = excluded = 0
            connection = db.connection().connection.driver_connection
            with connection.cursor() as cursor, cursor.copy(f"COPY trait_fixture_stage ({columns}) FROM STDIN") as copy:
                with (seed / filename).open(newline="") as stream:
                    for row in csv.DictReader(stream):
                        if kind == "gifts":
                            reference = row["contact_external_id"]
                        else:
                            # Verified generator format: call-{person_index+1:08d}-{call_number+1:03d}.
                            match = re.fullmatch(r"call-(\d{8})-\d+", row["external_id"])
                            reference = f"crm-{match[1]}" if match else ""
                        profile = mapping.get(reference)
                        if profile is None:
                            excluded += 1
                            continue
                        try:
                            if kind == "gifts":
                                amount = Decimal(row["amount"])
                                if not amount.is_finite():
                                    raise ValueError("Nonfinite gift amount")
                                values = (sid, profile, row["external_id"], amount, row["currency"],
                                          _date(row["gift_date"], None), row["fund"], row["campaign"],
                                          row["appeal_code"], row["channel"], row["payment_method"],
                                          row["is_recurring"].lower() == "true", row["recurring_plan_id"] or None)
                            else:
                                from psycopg.types.json import Jsonb
                                values = (sid, profile, row["type"], row["name"],
                                          _datetime(row["occurred_at"], None),
                                          _datetime(row["received_at"], None), row["message_id"],
                                          Jsonb(json.loads(row["properties"])), Jsonb(json.loads(row["context"])))
                        except (ValueError, InvalidOperation):
                            rejected += 1
                            continue
                        copy.write_row(values)
                        copied += 1
            inserted = db.execute(text(f"INSERT INTO {kind} ({columns}) SELECT {columns} FROM trait_fixture_stage ON CONFLICT DO NOTHING")).rowcount
            db.commit()
            report["copied"][kind] = copied
            report["inserted"][kind] = inserted
            report["rejected"][kind] = dict(invalid_selected=rejected, outside_selected_or_no_crm_reference=excluded)
            report["timings"][kind] = time.perf_counter() - begin
            (output / "traits-setup.json").write_text(json.dumps(report, indent=2))
        db.execute(text("ANALYZE"))
        db.commit()
        report["counts"] = {table: db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                            for table in ("profiles", "source_records", "gifts", "events")}
        report["active_profiles"] = db.execute(text(
            "SELECT count(*) FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted")).scalar_one()
    report["seconds"] = time.perf_counter() - started
    (output / "traits-setup.json").write_text(json.dumps(report, indent=2))
    return report


def measure_traits(engine, output: Path):
    from sqlalchemy import event, text
    from sqlalchemy.orm import Session
    from app.jobs import handlers
    from app.traits import engine as traits
    from app.traits.registry import TRAITS_BY_KEY
    from app.traits.verification import reference
    from app.dashboards import rollups
    pinned = date(2024, 12, 31)
    report = dict(as_of=str(pinned), status="running", timings={}, sql_seconds={},
                  setup=json.loads((output / "traits-setup.json").read_text()), discrepancies=[],
                  cpu_quota=Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
                  memory_limit=Path("/sys/fs/cgroup/memory.max").read_text().strip())

    def checkpoint():
        (output / "traits-measurement.json").write_text(json.dumps(report, default=str, indent=2))

    def timed(label, fn):
        start = time.perf_counter()
        value = fn()
        report["timings"][label] = time.perf_counter() - start
        checkpoint()
        return value

    def before(conn, cursor, statement, parameters, context, executemany):
        context.trait_benchmark_start = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, executemany):
        label = ("traits" if "INSERT INTO profile_traits" in statement else
                 "snapshots" if "INSERT INTO trait_snapshots" in statement else "other")
        report["sql_seconds"][label] = report["sql_seconds"].get(label, 0) + time.perf_counter() - context.trait_benchmark_start

    original_recompute, original_backfill = traits.recompute_traits, traits.backfill_trait_snapshots
    original_rollups = rollups.refresh_dashboard_rollups
    checkpoint()
    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    start_clock = datetime.now(timezone.utc)
    try:
        with patch.object(traits, "recompute_traits", lambda db, **kwargs: timed(
                "recompute_including_snapshot_rollups", lambda: original_recompute(db, as_of=pinned, **kwargs))), \
             patch.object(traits, "backfill_trait_snapshots", lambda db: timed(
                "historical_snapshots", lambda: original_backfill(db, as_of=pinned))), \
             patch.object(rollups, "refresh_dashboard_rollups", lambda db, **kwargs: timed(
                "dashboard_rollups", lambda: original_rollups(db, **kwargs))):
            timed("full_production_handler_including_commit",
                  lambda: handlers.run("traits.recompute", {"mode": "full"}))
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
    end_clock = datetime.now(timezone.utc)
    with Session(engine) as db:
        ids = list(db.execute(text("SELECT id FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted ORDER BY id")).scalars())
        sample = sorted(random.Random(20250308).sample(ids, min(1000, len(ids))))
        expected = timed("independent_python_reference", lambda: reference(db, sample, pinned))
        rows = list(db.execute(text("SELECT * FROM profile_traits WHERE profile_id=ANY(:ids)"),
                               {"ids": sample}).mappings())
        if len(rows) != len(sample):
            raise AssertionError("Missing traits on sampled profiles")
        for row in rows:
            for key in TRAITS_BY_KEY:
                ok = (start_clock <= row[key] <= end_clock if key == "computed_at"
                      else row[key] == expected[row["profile_id"]][key])
                if not ok:
                    report["discrepancies"].append(dict(profile_id=row["profile_id"], key=key,
                                                       actual=row[key], expected=expected[row["profile_id"]].get(key)))
        report.update(compared_profiles=len(sample), compared_traits=list(TRAITS_BY_KEY),
                      active_profiles=len(ids),
                      trait_rows=db.execute(text("SELECT count(*) FROM profile_traits")).scalar_one(),
                      snapshot_months=db.execute(text("SELECT count(DISTINCT month) FROM trait_snapshots")).scalar_one(),
                      database_bytes=db.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
        original_recompute(db, as_of=pinned, profile_ids=sample[:17], write_snapshot=False)
        for row in db.execute(text("SELECT * FROM profile_traits WHERE profile_id=ANY(:ids)"),
                              {"ids": sample[:17]}).mappings():
            for key in ("rfm_recency", "rfm_frequency", "rfm_monetary", "rfm_score"):
                if row[key] != expected[row["profile_id"]][key]:
                    report["discrepancies"].append(dict(profile_id=row["profile_id"], key="incremental_" + key))
        db.rollback()
    report["status"] = "passed" if not report["discrepancies"] and report["trait_rows"] == len(ids) else "failed"
    checkpoint()
    if report["status"] != "passed":
        raise AssertionError("Trait comparison failed: inspect traits-measurement.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("incremental-prepare", "incremental-import", "traits-prepare", "traits-measure"))
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records", type=int, default=50000)
    parser.add_argument("--profiles", type=int, default=250000)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.phase == "incremental-prepare":
        result = prepare_incremental(args.seed_dir, args.output, args.records)
    else:
        engine = isolated_engine()
        try:
            if args.phase == "incremental-import":
                result = import_incremental(engine, args.output)
            elif args.phase == "traits-prepare":
                result = prepare_traits(engine, args.seed_dir, args.output, args.profiles)
            else:
                result = measure_traits(engine, args.output)
        finally:
            engine.dispose()
    print(json.dumps(result, default=str), flush=True)


if __name__ == "__main__":
    main()