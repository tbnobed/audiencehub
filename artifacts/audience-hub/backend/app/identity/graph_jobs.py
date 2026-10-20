"""Scheduled, isolated-scratch Splink jobs with durable history and dry runs."""
import json
import re
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.db import engine, control_engine, CONNECT_ARGS, identify_connection, reject_stopping_sql
from app.identity.addresses import parse_address, refresh_addresses, refresh_households
from app.identity.locking import acquire_resolver_lock
from app.identity.matching import MODEL, NICKNAMES, conflict, link, settings
from app.jobs.queue import enqueue
from app.privacy.service import audit


def queue_run(db, dry_run=True):
    identifier = db.scalar(text("""
      INSERT INTO identity_graph_runs(dry_run) VALUES(:dry) RETURNING id
    """), {"dry": dry_run})
    job = enqueue(db, "identity.graph", {"run_id": identifier})
    db.execute(text("UPDATE identity_graph_runs SET job_id=:j WHERE id=:id"),
               {"j": job.id, "id": identifier})
    return job.id


def drop_scratch(run_id):
    from app.jobs.lifecycle import control
    schema = f"kinship_graph_{int(run_id)}"
    with control(), control_engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@contextmanager
def scratch(run_id, db):
    """Per-run schema; PostgresAPI cannot leak work tables into application schema."""
    schema = f"kinship_graph_{int(run_id)}"
    scratch_engine = create_engine(engine.url, pool_size=2, max_overflow=0,
                                   hide_parameters=True, connect_args=CONNECT_ARGS)
    event.listen(scratch_engine, "checkout", identify_connection)
    event.listen(scratch_engine, "before_cursor_execute", reject_stopping_sql)

    @event.listens_for(scratch_engine, "connect")
    def search_path(connection, _):
        # Splink's UDFs/queries are unqualified; set per-connection, not per SQL statement.
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}",public')
        connection.commit()

    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    try:
        yield scratch_engine, schema
        # The application transaction reads the prediction table, so it holds
        # an ACCESS SHARE lock. Drop on that SAME transaction: a second
        # connection would wait on our own lock until timeout.
        db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    except BaseException:
        db.rollback()
        scratch_engine.dispose()
        drop_scratch(run_id)
        raise
    finally:
        scratch_engine.dispose()


def stage(db, scratch_engine):
    """No pandas download and no write to live contacts during a dry run."""
    with scratch_engine.begin() as connection:
        connection.execute(text("""CREATE TABLE inputs(
          unique_id bigint PRIMARY KEY,first_name text,nickname text,last_name text,
          address text,address_key text,zip5 text,dob text,phone text)"""))
    after = 0
    while True:
        rows = db.execute(text("""
          SELECT p.*,s.address2,s.dob,a.normalized_key AS vendor_key,a.parser AS address_parser,
            a.number AS vendor_number,a.street AS vendor_street,a.unit AS vendor_unit,
            a.zip5 AS vendor_zip5,a.input_hash AS address_input_hash
          FROM active_profiles p
          LEFT JOIN identity_addresses a ON a.profile_id=p.id
          LEFT JOIN LATERAL (
            SELECT (array_agg(sr.address2 ORDER BY so.priority,sr.updated_at DESC,sr.id)
              FILTER(WHERE sr.address1=p.address1 AND sr.address2 IS NOT NULL))[1] AS address2,
              (array_agg(coalesce(sr.attributes->>'date_of_birth',sr.attributes->>'dob')
                ORDER BY so.priority,sr.updated_at DESC,sr.id)
                FILTER(WHERE coalesce(sr.attributes->>'date_of_birth',sr.attributes->>'dob') IS NOT NULL))[1] AS dob
            FROM source_records sr JOIN sources so ON so.id=sr.source_id
            WHERE sr.profile_id=p.id AND (so.license_expires_at IS NULL OR so.license_expires_at>=current_date)
          ) s ON true WHERE p.id>:after ORDER BY p.id LIMIT 1000
        """), {"after": after}).mappings().all()
        if not rows:
            break
        payload = []
        for row in rows:
            address = parse_address(dict(row))
            # A vendor-validated address is valid only for the same raw input;
            # refresh_addresses invalidates old results on live runs.
            if row["address_parser"] == "vendor":
                import hashlib
                input_hash = hashlib.sha256(json.dumps([
                    (row.get(k) or "") if k == "address2" else row.get(k)
                    for k in ("address1", "address2", "city", "region", "postal_code", "country")
                ]).encode()).hexdigest()
                if input_hash == row["address_input_hash"]:
                    address.update(normalized_key=row["vendor_key"],number=row["vendor_number"],
                                   street=row["vendor_street"],unit=row["vendor_unit"],zip5=row["vendor_zip5"])
            first = str(row["first_name"] or "").strip().lower() or None
            dob = str(row["dob"] or "").strip()[:10]
            # Invalid/partial DOB is missing evidence, never a shared empty value.
            from datetime import date
            try:
                dob = date.fromisoformat(dob).isoformat()
            except ValueError:
                dob = None
            payload.append(dict(id=row["id"], first=first, nickname=NICKNAMES.get(first, first),
                last=str(row["last_name"] or "").strip().lower() or None,
                address=" ".join(str(address[k] or "") for k in ("number", "street", "unit", "zip5"))
                    if address["normalized_key"] else None,
                key=address["normalized_key"], zip=address["zip5"], dob=dob, phone=row["phone"]))
        with scratch_engine.begin() as connection:
            connection.execute(text("""INSERT INTO inputs VALUES(
              :id,:first,:nickname,:last,:address,:key,:zip,:dob,:phone)"""), payload)
        after = rows[-1]["id"]
    with scratch_engine.begin() as connection:
        for column in ("address_key", "phone", "last_name,zip5"):
            connection.execute(text(f"CREATE INDEX ON inputs({column})"))
        connection.execute(text("ANALYZE inputs"))


def count_sources(db, left, right, band):
    """Count each candidate once per represented source, not once per record."""
    db.execute(text("""
      INSERT INTO identity_graph_daily(day,source_id,candidates,linked,review)
      SELECT current_date,source_id,0,CAST(:linked AS int),CAST(:review AS int)
      FROM source_records WHERE profile_id=ANY(:ids) GROUP BY source_id
      ON CONFLICT(day,source_id) DO UPDATE SET
        linked=identity_graph_daily.linked+EXCLUDED.linked,
        review=identity_graph_daily.review+EXCLUDED.review
    """), {"ids": [left, right], "linked": band == "linked", "review": band in ("review", "conflict")})


def run(run_id, job_id=None):
    # History is committed independently so a failed model run remains visible.
    with Session(engine) as progress:
        row = progress.execute(text("SELECT * FROM identity_graph_runs WHERE id=:id"),
                               {"id": run_id}).mappings().one()
        if row["status"] == "succeeded":
            return
        dry_run = row["dry_run"]
        progress.execute(text("""UPDATE identity_graph_runs SET status='running',
          started_at=clock_timestamp(),finished_at=NULL,error=NULL WHERE id=:id"""), {"id": run_id})
        progress.commit()
    counts = dict(auto=0, review=0, rejected=0, conflicts=0, linked=0)
    try:
        with Session(engine) as db:
            acquire_resolver_lock(db)
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"))
            policy = db.execute(text("SELECT * FROM identity_graph_policy WHERE id=1")).mappings().one()
            if not dry_run:
                refresh_addresses(db)
                refresh_households(db)
                db.execute(text("DELETE FROM identity_devices WHERE expires_at<=now()"))
            with scratch(run_id, db) as (scratch_engine, schema):
                stage(db, scratch_engine)
                from splink import Linker
                from splink.backends.postgres import PostgresAPI
                api = PostgresAPI(engine=scratch_engine, schema=schema)
                linker = Linker(api.register("inputs"), settings())
                prediction = linker.inference.predict(threshold_match_probability=0)
                # Physical name is library-generated, never client-controlled.
                name = prediction.physical_name
                if not re.fullmatch(r"[A-Za-z0-9_.\"]+", name):
                    raise ValueError("Unexpected Splink result relation")
                table_name = name.split(".")[-1].strip('"')
                if not re.fullmatch(r"[A-Za-z0-9_]+", table_name):
                    raise ValueError("Unexpected Splink result relation")
                qualified = f'"{schema}"."{table_name}"'
                counts["rejected"] = db.scalar(text(f"""
                  SELECT count(*) FROM {qualified} WHERE match_probability<:lower
                """), {"lower": policy["review_threshold"]})
                if not dry_run:
                    # Aggregate all blocked pairs by source in SQL. Millions of
                    # below-threshold pairs must not become millions of Python
                    # queries or review writes.
                    db.execute(text(f"""
                      WITH represented AS (
                        SELECT p.unique_id_l,p.unique_id_r,s.source_id FROM {qualified} p
                        JOIN source_records s ON s.profile_id=p.unique_id_l
                        UNION
                        SELECT p.unique_id_l,p.unique_id_r,s.source_id FROM {qualified} p
                        JOIN source_records s ON s.profile_id=p.unique_id_r
                      ) INSERT INTO identity_graph_daily(day,source_id,candidates)
                      SELECT current_date,source_id,count(*) FROM represented GROUP BY source_id
                      ON CONFLICT(day,source_id) DO UPDATE SET
                        candidates=identity_graph_daily.candidates+EXCLUDED.candidates
                    """))
                with scratch_engine.connect() as connection:
                    result = connection.execution_options(stream_results=True).execute(text(
                        f"SELECT unique_id_l,unique_id_r,match_probability FROM {name} "
                        "WHERE match_probability>=:lower "
                        "ORDER BY match_probability DESC,unique_id_l,unique_id_r"),
                        {"lower": policy["review_threshold"]})
                    for left, right, score in result:
                        left, right = sorted((int(left), int(right)))
                        score = float(score)
                        # Earlier links in this run can retire a candidate. Never
                        # use stale scores transitively through a merged person.
                        if db.scalar(text("""SELECT count(*) FROM active_profiles
                          WHERE id=ANY(:ids)"""), {"ids": [left, right]}) != 2:
                            continue
                        blocked = conflict(db, left, right)
                        band = "conflict" if blocked else ("linked" if score >= policy["auto_threshold"] else "review")
                        counts["conflicts" if blocked else "auto" if band == "linked" else "review"] += 1
                        if dry_run:
                            continue
                        # A human non-match/unsure is not overwritten by a daily
                        # batch. Explicit new review is needed to reconsider it.
                        old = db.execute(text("""SELECT id,verdict,status FROM identity_pairs
                          WHERE left_id=:l AND right_id=:r FOR UPDATE"""), {"l": left, "r": right}).mappings().first()
                        if old and (old["verdict"] is not None or old["status"] == "linked"):
                            continue
                        count_sources(db, left, right, band)
                        merge_id = link(db, left, right, score, MODEL, job_id) if band == "linked" else None
                        counts["linked"] += int(merge_id is not None)
                        db.execute(text("""
                          INSERT INTO identity_pairs(left_id,right_id,confidence,rule,status,run_id,merge_id)
                          VALUES(:l,:r,:score,:rule,:status,:run,:merge)
                          ON CONFLICT(left_id,right_id) DO UPDATE SET confidence=EXCLUDED.confidence,
                            status=EXCLUDED.status,run_id=EXCLUDED.run_id,merge_id=EXCLUDED.merge_id,
                            rule=EXCLUDED.rule,updated_at=now()
                        """), {"l": left, "r": right, "score": score, "rule": MODEL,
                               "status": band, "run": run_id, "merge": merge_id})
                    result.close()
            if not dry_run:
                refresh_addresses(db)
                refresh_households(db)
                from app.identity.quality import refresh_queue_trends
                refresh_queue_trends(db)
                if counts["linked"]:
                    enqueue(db, "traits.recompute", {"mode": "dirty"}, dedupe_key="traits:incremental")
            db.execute(text("""UPDATE identity_graph_runs SET status='succeeded',finished_at=clock_timestamp(),
              counts=CAST(:counts AS jsonb) WHERE id=:id"""), {"id": run_id, "counts": json.dumps(counts)})
            audit(db, None, "identity.graph.dry_run" if dry_run else "identity.graph.run",
                  "identity_graph_run", run_id, counts)
            db.commit()
    except Exception as exc:
        # Provider/SQL exceptions may include names/addresses. Keep safe history;
        # queue error also uses this constant, not the raw exception/PII.
        from app.identity.locking import IdentityResolutionDeferred
        from app.jobs.lifecycle import control, OwnershipLost
        deferred = isinstance(exc, IdentityResolutionDeferred)
        cancelled = isinstance(exc, OwnershipLost)
        # Also cover failure of the final application COMMIT, which can roll
        # back the transactional DROP and otherwise restore scratch PII.
        drop_scratch(run_id)
        with control(), Session(control_engine) as progress:
            progress.execute(text("""UPDATE identity_graph_runs SET status='failed',
              finished_at=clock_timestamp(),counts=CAST(:counts AS jsonb),
              error='Identity graph job did not complete; no graph changes committed.'
              WHERE id=:id"""), {"id": run_id, "counts": json.dumps(counts)})
            progress.commit()
        if deferred:
            with control(), Session(control_engine) as progress:
                progress.execute(text("""UPDATE identity_graph_runs SET status='queued',error=NULL,
                  finished_at=NULL WHERE id=:id"""), {"id": run_id})
                progress.commit()
            raise
        if cancelled:
            raise
        raise RuntimeError("Identity graph job failed; inspect model configuration and PostgreSQL permissions. No graph changes committed.") from None


def purge_scratch(db):
    """Erasure must also clear scratch data left by a killed worker.

    Caller holds privacy-exclusive lock. Active match runs hold privacy-shared,
    so none of these schemas can belong to a live successful batch.
    """
    names = db.scalars(text("""SELECT nspname FROM pg_namespace
      WHERE nspname ~ '^kinship_graph_[0-9]+$'""")).all()
    for schema in names:
        if re.fullmatch(r"kinship_graph_[0-9]+", schema):
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
