"""Attachment semantics, run only by the disposable acceptance harness."""
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine
from app.identity.attachments import attach_transactions
from app.identity.bulk import resolve_bulk
from test_consent_ingestion_pg import db

pytestmark = pytest.mark.skipif(
    os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
    reason="requires disposable benchmark PostgreSQL",
)


def test_gifts_never_create_people_ambiguous_and_no_match_are_terminal(db):
    key = "attach-" + uuid4().hex
    with Session(db.get_bind()) as db:
        source = db.scalar(text("""
            INSERT INTO sources(key,name,kind) VALUES(:key,'Attachment test','csv') RETURNING id
        """), {"key": key})
        contact_import = db.scalar(text("""
            INSERT INTO imports(source_id,filename,record_type,status)
            VALUES(:s,'contacts.csv','contact','completed') RETURNING id
        """), {"s": source})
        gift_import = db.scalar(text("""
            INSERT INTO imports(source_id,filename,record_type,status)
            VALUES(:s,'gifts.csv','gift','completed') RETURNING id
        """), {"s": source})
        # Two duplicate raw contacts produce one connected component, NOT a
        # profile_merges row: no pre-existing loser profile ever existed.
        for ext in ("contact-a", "contact-a-copy"):
            db.execute(text("""
                INSERT INTO source_records(source_id,external_id,email_norm,raw_hash,last_import_id)
                VALUES(:s,:ext,:email,:ext,:i)
            """), {"s": source, "ext": ext, "email": key+"@example.org", "i": contact_import})
        result = resolve_bulk(db, mode="full")
        assert result == {"records": 2, "profiles_created": 1, "merges": 0}
        winner = db.scalar(text("SELECT profile_id FROM source_records WHERE source_id=:s LIMIT 1"), {"s": source})
        other = db.scalar(text("INSERT INTO profiles DEFAULT VALUES RETURNING id"))
        phone = "+1999" + str(source).zfill(7)
        db.execute(text("INSERT INTO identifiers(type,value,profile_id) VALUES('phone',:phone,:p)"),
                   {"phone": phone, "p": other})
        cases = [
            ("reference", key+"@example.org", phone, {"_identity_reference": {
                "source_key": key, "contact_external_id": "contact-a"}}, winner),
            ("email", key+"@example.org", None, {}, winner),
            ("ambiguous", key+"@example.org", phone, {}, None),
            ("no-match", key+"-missing@example.org", None, {}, None),
        ]
        for ext, email, number, attrs, expected in cases:
            record = db.scalar(text("""
                INSERT INTO source_records(source_id,external_id,email_norm,phone_e164,
                  attributes,raw_hash,last_import_id)
                VALUES(:s,:ext,:email,:phone,CAST(:attrs AS jsonb),:ext,:i) RETURNING id
            """), {"s": source, "ext": ext, "email": email, "phone": number,
                   "attrs": json.dumps(attrs), "i": gift_import})
            db.execute(text("""
                INSERT INTO gifts(source_id,external_id,source_record_id,amount)
                VALUES(:s,:ext,:r,10)
            """), {"s": source, "ext": ext, "r": record})
        assert resolve_bulk(db)["records"] == 0
        attach_transactions(db)
        attached = dict(db.execute(text("SELECT external_id,profile_id FROM gifts WHERE source_id=:s"),
                                   {"s": source}).all())
        assert attached == {case[0]: case[-1] for case in cases}
        assert db.scalar(text("SELECT count(*) FROM source_records WHERE source_id=:s AND resolved_at IS NULL"),
                         {"s": source}) == 0
        assert db.scalar(text("SELECT count(*) FROM gifts WHERE source_id=:s AND profile_id IS NULL"),
                         {"s": source}) == 2
        # A later small gift attaches without ever entering the graph.
        db.execute(text("""
            INSERT INTO source_records(source_id,external_id,email_norm,raw_hash,last_import_id)
            VALUES(:s,'later',:email,'later',:i)
        """), {"s": source, "email": key+"@example.org", "i": gift_import})
        db.execute(text("""
            INSERT INTO gifts(source_id,external_id,source_record_id,amount)
            SELECT source_id,external_id,id,20 FROM source_records
            WHERE source_id=:s AND external_id='later'
        """), {"s": source, "email": key+"@example.org", "i": gift_import})
        attach_transactions(db)
        assert db.scalar(text("SELECT profile_id FROM gifts WHERE source_id=:s AND external_id='later'"),
                         {"s": source}) == winner
        db.rollback()


def test_event_only_and_deleted_owner_are_terminal_without_resurrection(db):
    key = "event-" + uuid4().hex
    with Session(db.get_bind()) as db:
        source = db.scalar(text("INSERT INTO sources(key,name,kind) VALUES(:k,'Events','csv') RETURNING id"),
                           {"k": key})
        imported = db.scalar(text("""
            INSERT INTO imports(source_id,filename,record_type,status)
            VALUES(:s,'events.csv','event','completed') RETURNING id
        """), {"s": source})
        deleted = db.scalar(text("INSERT INTO profiles(is_deleted) VALUES(true) RETURNING id"))
        db.execute(text("INSERT INTO identifiers(type,value,profile_id) VALUES('email',:v,:p)"),
                   {"v": key+"@example.org", "p": deleted})
        for ext, email in (("deleted", key+"@example.org"), ("missing", key+"-missing@example.org")):
            db.execute(text("""
                INSERT INTO source_records(source_id,external_id,email_norm,raw_hash,last_import_id)
                VALUES(:s,:ext,:email,:ext,:i)
            """), {"s": source, "ext": ext, "email": email, "i": imported})
            db.execute(text("""
                INSERT INTO events(source_id,type,name,message_id,occurred_at)
                VALUES(:s,'track','Call',:ext,'2025-01-01'::timestamptz)
            """), {"s": source, "ext": ext, "email": email, "i": imported})
        assert resolve_bulk(db)["records"] == 0
        attach_transactions(db)
        assert db.scalar(text("SELECT count(*) FROM source_records WHERE source_id=:s AND resolved_at IS NULL"),
                         {"s": source}) == 0
        assert db.scalar(text("SELECT count(*) FROM events WHERE source_id=:s AND profile_id IS NULL"),
                         {"s": source}) == 2
        db.rollback()


@pytest.mark.parametrize("rows_total,expected_mode", [(9, "incremental"), (10, "full"), (100001, "full")])
def test_automatic_import_cohort_has_one_pipeline_and_shared_traits(db, monkeypatch, rows_total, expected_mode):
    """Three independent completion wakeups share one persisted pipeline."""
    import app.db
    import app.identity.bulk
    import app.identity.attachments
    from app.jobs.handlers import run
    from app.jobs.queue import enqueue

    local = db.get_bind()
    monkeypatch.setattr(app.db, "engine", local)
    source = db.scalar(text("INSERT INTO sources(key,name,kind) VALUES('cohort','Cohort','csv') RETURNING id"))
    baseline_import = db.scalar(text("""
        INSERT INTO imports(source_id,filename,record_type,status)
        VALUES(:s,'baseline','contact','completed') RETURNING id
    """), {"s": source})
    db.execute(text("""
        INSERT INTO source_records(source_id,external_id,raw_hash,last_import_id,resolved_at)
        SELECT :s,'baseline-'||n,'baseline-'||n,:i,now() FROM generate_series(1,98)n
    """), {"s": source, "i": baseline_import})
    imports = []
    jobs = []
    for n in range(3):
        imported = db.scalar(text("""
            INSERT INTO imports(source_id,filename,record_type,status,rows_total)
            VALUES(:s,:file,'contact','completed',:rows) RETURNING id
        """), {"s": source, "file": str(n), "rows": rows_total})
        db.execute(text("""
            INSERT INTO source_records(source_id,external_id,email_norm,raw_hash,last_import_id)
            VALUES(:s,:ext,'cohort@example.org',:ext,:i)
        """), {"s": source, "ext": str(n), "i": imported})
        imports.append(imported)
        jobs.append(enqueue(db, "identity.resolve_batch", {"import_id": imported},
                            dedupe_key=f"identity:import:{imported}").id)
    db.commit()
    calls = []
    original_resolve, original_attach = app.identity.bulk.resolve_bulk, app.identity.attachments.attach_transactions

    def resolve(session, **kwargs):
        calls.append(("resolve", kwargs["mode"]))
        return original_resolve(session, **kwargs)

    def attach(session, **kwargs):
        calls.append("attach")
        return original_attach(session, **kwargs)

    monkeypatch.setattr(app.identity.bulk, "resolve_bulk", resolve)
    monkeypatch.setattr(app.identity.attachments, "attach_transactions", attach)
    for imported, job_id in zip(imports, jobs):
        run("identity.resolve_batch", {"import_id": imported}, job_id=job_id)
    assert calls == [("resolve", expected_mode), "attach"]
    batches = db.scalars(text("SELECT DISTINCT identity_batch_id FROM imports WHERE id=ANY(:ids)"),
                         {"ids": imports}).all()
    assert len(batches) == 1 and batches[0] is not None
    child = db.execute(text("SELECT payload FROM jobs WHERE type='traits.recompute'")).scalar_one()
    assert child["batch_id"] == batches[0]
    assert db.scalar(text("SELECT count(*) FROM jobs WHERE type='identity.resolve_batch' AND payload->>'batch_id'=:id"),
                     {"id": str(batches[0])}) == 3
    assert db.scalar(text("SELECT count(DISTINCT profile_id) FROM source_records WHERE last_import_id=ANY(:ids)"),
                     {"ids": imports}) == 1
    progress = db.scalar(text("SELECT progress FROM jobs WHERE id=:id"), {"id": jobs[0]})
    assert set(progress["attachment_steps"]) == {
        "drop_assignment", "assignment", "index_assignment", "analyze_assignment",
        "gifts", "events", "source_records", "dirty_profiles",
    }
    assert all(step["status"] == "completed" and step["elapsed_ms"] >= 0
               for step in progress["attachment_steps"].values())
    assert {"resolving", "attaching"} <= set(progress["phase_elapsed_ms"])


@pytest.mark.parametrize("profiles", [10, 500])
def test_seed_preregisters_all_five_and_defers_until_last_import(db, monkeypatch, tmp_path, profiles):
    import time
    import app.db
    import app.importer
    import app.load_status
    import app.identity.bulk
    from app.seed import generate_seed
    from app.imports.service import run_import
    from app.identity.locking import IdentityResolutionDeferred
    from app.jobs.handlers import run
    from app.jobs.queue import succeed

    local = db.get_bind()
    monkeypatch.setattr(app.db, "engine", local)
    monkeypatch.setattr(app.importer, "engine", local)
    files = generate_seed(profiles=profiles, scale="medium", output_dir=tmp_path / "seed",
                          mode="legacy")["files"]
    calls = []
    original = app.identity.bulk.resolve_bulk

    def resolve(session, **kwargs):
        assert session.scalar(text("SELECT count(*) FROM imports WHERE status<>'completed'")) == 0
        calls.append(kwargs["mode"])
        return original(session, **kwargs)

    monkeypatch.setattr(app.identity.bulk, "resolve_bulk", resolve)

    def drain(bind, **kwargs):
        assert bind is local
        with Session(local) as session:
            assert session.scalar(text("SELECT count(*) FROM imports")) == 5
            assert session.scalar(text("SELECT count(DISTINCT identity_batch_id) FROM imports")) == 1
            importing = session.execute(text("""
                SELECT id,(payload->>'import_id')::bigint FROM jobs WHERE type='import.run' ORDER BY id
            """)).all()
            assert len(importing) == 5
            batch = session.scalar(text("SELECT id FROM identity_batches"))
        for job_id, import_id in importing:
            with pytest.raises(IdentityResolutionDeferred):
                run("identity.resolve_batch", {"batch_id": batch})
            with Session(local) as session:
                session.execute(text("UPDATE jobs SET status='running' WHERE id=:id"), {"id": job_id})
                session.commit()
                run_import(import_id, db=session)
                succeed(session, job_id)
                session.commit()
        run("identity.resolve_batch", {"batch_id": batch})
        with Session(local) as session:
            assert session.scalar(text("SELECT count(*) FROM source_records WHERE resolved_at IS NULL")) == 0
            assert session.scalar(text("SELECT count(*) FROM jobs WHERE type='traits.recompute'")) == 1
            assert session.scalar(text("SELECT phase FROM identity_batches")) == "completed"
            if profiles == 500:
                traits_job = session.scalar(text("SELECT id FROM jobs WHERE type='traits.recompute'"))
                session.execute(text("UPDATE jobs SET status='running' WHERE id=:id"), {"id": traits_job})
                session.commit()
        if profiles == 500:
            started = time.perf_counter()
            run("traits.recompute", {"mode": "full", "batch_id": batch}, job_id=traits_job)
            elapsed = time.perf_counter() - started
            with Session(local) as session:
                succeed(session, traits_job)
                session.commit()
                assert session.scalar(text("SELECT count(*) FROM profile_traits")) > 400
                assert session.scalar(text("SELECT count(*) FROM dashboard_giving_daily")) > 0
                progress = session.scalar(text("SELECT progress FROM jobs WHERE id=:id"), {"id": traits_job})
                assert {"traits", "rollups"} <= set(progress["phase_elapsed_ms"])
            print("REAL_500_FULL_TRAITS_ROLLUPS_SECONDS", elapsed)
            assert elapsed < 10
        return 0

    monkeypatch.setattr(app.load_status, "run_load_status", drain)
    started = time.perf_counter()
    ids = app.importer.import_seed_files(files)
    print("REAL_SEED_PIPELINE_SECONDS", profiles, time.perf_counter() - started)
    assert len(ids) == 5 and calls == ["full"]


@pytest.mark.parametrize("explicit", [False, True])
def test_failed_partial_import_blocks_identity_without_discarding_checkpoint(db, monkeypatch, explicit):
    import app.db
    from app.jobs.handlers import run
    monkeypatch.setattr(app.db, "engine", db.get_bind())
    source = db.scalar(text("INSERT INTO sources(key,name,kind) VALUES('partial','Partial','csv') RETURNING id"))
    batch = db.scalar(text("INSERT INTO identity_batches(phase) VALUES('importing') RETURNING id"))
    imported = db.scalar(text("""
        INSERT INTO imports(source_id,filename,record_type,status,identity_batch_id,last_committed_record_number)
        VALUES(:s,'partial.csv','contact','failed',:b,101) RETURNING id
    """), {"s": source, "b": batch if explicit else None})
    db.execute(text("""
        INSERT INTO source_records(source_id,external_id,raw_hash,last_import_id)
        VALUES(:s,'saved','saved',:i)
    """), {"s": source, "i": imported})
    db.commit()
    with pytest.raises(ValueError, match="partially committed"):
        run("identity.resolve_batch", {"batch_id": batch} if explicit else {"import_id": imported})
    assert db.scalar(text("SELECT last_committed_record_number FROM imports WHERE id=:id"), {"id": imported}) == 101
    assert db.scalar(text("SELECT count(*) FROM source_records WHERE last_import_id=:id AND resolved_at IS NULL"),
                     {"id": imported}) == 1


def test_batch_completion_timestamp_is_after_work_not_transaction_start(db, monkeypatch):
    import app.db
    import app.identity.bulk
    from app.jobs.handlers import run

    monkeypatch.setattr(app.db, "engine", db.get_bind())
    batch = db.scalar(text("INSERT INTO identity_batches(phase) VALUES('importing') RETURNING id"))
    db.commit()
    observed = {}

    def delayed_resolution(session, **kwargs):
        observed["transaction_start"] = session.scalar(text("SELECT transaction_timestamp()"))
        session.execute(text("SELECT pg_sleep(0.15)"))
        observed["after_work"] = session.scalar(text("SELECT clock_timestamp()"))
        return {"records": 0, "profiles_created": 0, "merges": 0}

    monkeypatch.setattr(app.identity.bulk, "resolve_bulk", delayed_resolution)
    run("identity.resolve_batch", {"batch_id": batch})
    row = db.execute(text("SELECT created_at,finished_at,phase FROM identity_batches WHERE id=:id"),
                     {"id": batch}).mappings().one()
    assert row["phase"] == "completed"
    assert row["created_at"] <= observed["transaction_start"] < observed["after_work"] <= row["finished_at"]
    assert (row["finished_at"] - observed["transaction_start"]).total_seconds() >= 0.15
