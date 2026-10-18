import time


class SegmentTraitsRefreshRequired(ValueError):
    """Safe, actionable failure when an audience's trait barrier cannot pass."""


def run(job_type: str, payload: dict, job_id: int | None = None) -> None:
    if job_type == "source.pull":
        from app.source_pulls import run_source
        from app.source_connectors import SourceError
        from app.identity.locking import IdentityResolutionDeferred
        from app.jobs.lifecycle import OwnershipLost
        try:
            run_source(int(payload["source_id"]), feed=payload.get("feed"), job_id=job_id)
        except (IdentityResolutionDeferred, OwnershipLost):
            raise
        except Exception:
            raise SourceError("Source pull failed; retry resumes committed checkpoints. Verify canonical data and connection configuration.") from None
        return
    if job_type == "ingestion.resolve":
        from app.ingestion.jobs import run_resolution
        run_resolution(int(payload["source_id"]), job_id=job_id)
        return
    if job_type == "activation.run":
        from app.activations.service import run_activation
        run_activation(int(payload["run_id"]), job_id=job_id)
        return
    if job_type == "deletion.execute":
        from app.privacy.jobs import run_deletion
        run_deletion(int(payload["request_id"]), job_id=job_id)
        return
    if job_type == "privacy.expire_uploads":
        from app.privacy.jobs import run_upload_retention
        run_upload_retention()
        return
    if job_type == "activation.expire_exports":
        from sqlalchemy.orm import Session
        from app.db import engine
        from app.activations.service import expire_exports
        with Session(engine) as db:
            expire_exports(db)
            db.commit()
        return
    if job_type == "segment.materialize":
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        from app.db import engine
        from app.segments.service import materialize
        from app.identity.locking import IdentityResolutionDeferred
        with Session(engine) as db:
            # Same transaction lock as recompute: never read half-old traits or
            # race a recompute between readiness checking and materialization.
            if not db.scalar(text(
                "SELECT pg_try_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"
            )):
                raise IdentityResolutionDeferred("Partner audience refresh waits for traits.")
            if payload.get("traits_window"):
                status = db.scalar(text("""SELECT j.status FROM scheduled_runs s
                    JOIN jobs j ON j.id=s.job_id
                    WHERE s.task='traits.nightly_full' AND s.window_key=:window"""),
                    {"window": payload["traits_window"]})
                if status in ("queued", "running"):
                    raise IdentityResolutionDeferred("Partner audience refresh waits for nightly traits.")
                if status != "succeeded":
                    raise SegmentTraitsRefreshRequired("Nightly traits did not succeed; repair traits and retry the partner audience refresh.")
            if db.scalar(text("""SELECT EXISTS(SELECT 1 FROM jobs
                WHERE type='traits.recompute' AND status IN ('queued','running'))""")):
                raise IdentityResolutionDeferred("Partner audience refresh waits for traits.")
            latest_full = db.scalar(text("""SELECT status FROM jobs
                WHERE type='traits.recompute' AND payload->>'mode'='full'
                ORDER BY id DESC LIMIT 1"""))
            if latest_full in ("failed", "cancelled"):
                raise SegmentTraitsRefreshRequired("Latest full traits refresh failed or was cancelled; repair traits before refreshing partner audiences.")
            materialize(db, int(payload["segment_id"]))
            db.commit()
        return
    if job_type == "identity.resolve_batch":
        from sqlalchemy.orm import Session

        from app.db import engine
        from app.identity.bulk import resolve_bulk
        from app.identity.attachments import attach_transactions
        from app.identity.statistics import refresh_identity_statistics
        from app.identity.locking import IdentityResolutionDeferred, acquire_resolver_lock
        from app.jobs.phase_progress import begin_phase, finish_phase, record_attachment_step
        from sqlalchemy import text

        with Session(engine) as db:
            acquire_resolver_lock(db)
            batch_id = payload.get("batch_id")
            if db.scalar(text("""SELECT EXISTS(SELECT 1 FROM source_connections
                WHERE state ? 'pending')""")):
                raise IdentityResolutionDeferred("Identity waits for source pull recovery.")
            if db.scalar(text("""SELECT EXISTS(SELECT 1 FROM jobs
                WHERE type IN ('import.run','source.pull') AND status IN ('queued','running'))""")):
                raise IdentityResolutionDeferred("Identity waits for the import barrier.")
            if not batch_id and payload.get("import_id"):
                batch_id = db.scalar(text("SELECT identity_batch_id FROM imports WHERE id=:id"),
                                     {"id": payload["import_id"]})
            if db.scalar(text("""
                SELECT EXISTS(SELECT 1 FROM imports i WHERE i.status IN ('failed','cancelled')
                  AND EXISTS(SELECT 1 FROM source_records sr
                    WHERE sr.last_import_id=i.id AND sr.resolved_at IS NULL))
            """)):
                raise ValueError("Identity waits for recovery of partially committed unsuccessful imports.")
            if batch_id:
                phase = db.scalar(text("SELECT phase FROM identity_batches WHERE id=:id FOR UPDATE"),
                                  {"id": batch_id})
                if phase == "completed":
                    return
                if phase == "registering" or db.scalar(text("""
                    SELECT EXISTS(SELECT 1 FROM imports WHERE identity_batch_id=:id
                      AND status NOT IN ('completed','failed','cancelled'))
                """), {"id": batch_id}):
                    raise IdentityResolutionDeferred("Identity batch registration/imports are incomplete.")
                if db.scalar(text("""SELECT EXISTS(SELECT 1 FROM imports
                    WHERE identity_batch_id=:id AND status<>'completed')"""), {"id": batch_id}):
                    raise ValueError("Identity batch contains unsuccessful imports; retry those imports first.")
                db.execute(text("UPDATE identity_batches SET phase='resolving' WHERE id=:id"), {"id": batch_id})
            # One complete set-based pass, never bounded-prefix bursts. Use
            # incremental write minimization only for genuinely small deltas.
            large = (db.scalar(text("SELECT mode FROM identity_batches WHERE id=:id"),
                               {"id": batch_id}) != "incremental") if batch_id else db.scalar(text("""
                WITH sizes AS MATERIALIZED (
                  SELECT last_import_id,count(*) AS records,
                         bool_or(resolved_at IS NULL) AS pending
                  FROM source_records GROUP BY last_import_id
                ), total AS (SELECT COALESCE(sum(records),0) AS records FROM sizes)
                SELECT EXISTS(SELECT 1 FROM sizes s JOIN imports i ON i.id=s.last_import_id
                  CROSS JOIN total t WHERE s.pending
                  AND (i.rows_total>100000 OR i.rows_total>=0.1*(t.records-s.records)))
            """))
            if not batch_id and payload.get("import_id"):
                batch_id = db.scalar(text("""
                    INSERT INTO identity_batches(phase,mode) VALUES('resolving',:mode) RETURNING id
                """), {"mode": "bulk" if large else "incremental"})
                # The exclusive resolver lock makes this a single durable
                # cohort. Remaining per-import wakeups find the completed batch
                # and share its traits child rather than rescan attachments.
                db.execute(text("""
                    UPDATE imports i SET identity_batch_id=:batch
                    WHERE i.identity_batch_id IS NULL AND i.status='completed'
                      AND EXISTS(SELECT 1 FROM jobs j WHERE j.type='identity.resolve_batch'
                        AND j.status IN ('queued','running')
                        AND j.payload->>'import_id'=i.id::text)
                """), {"batch": batch_id})
            begin_phase(engine, job_id, "resolving")
            resolve_bulk(db, job_id=job_id, mode="full" if large else "incremental")
            if batch_id:
                db.execute(text("UPDATE identity_batches SET phase='attaching' WHERE id=:id"), {"id": batch_id})
            begin_phase(engine, job_id, "attaching")
            attach_transactions(db, progress_callback=lambda step, status, elapsed_ms:
                                record_attachment_step(engine, job_id, step, status, elapsed_ms))
            # Imported FK statistics were collected while profile_id was NULL.
            # Publish accurate distributions before any traits child can run.
            refresh_identity_statistics(db)
            if batch_id:
                db.execute(text("UPDATE identity_batches SET phase='completed',finished_at=clock_timestamp() WHERE id=:id"),
                           {"id": batch_id})
            # Publish fresh rollups only after the import's identity queue has
            # drained. The identity job id prevents a running earlier refresh
            # from swallowing this later completion.
            from app.jobs.queue import enqueue
            scope = {key: payload[key] for key in ("batch_id", "import_id") if key in payload}
            if batch_id:
                scope["batch_id"] = batch_id
                # Publish job coverage after the phase telemetry writes, which
                # use their own connection and must not wait on our row lock.
                db.execute(text("""
                    UPDATE jobs j SET payload=j.payload || jsonb_build_object('batch_id',CAST(:batch AS bigint))
                    FROM imports i WHERE i.identity_batch_id=:batch
                      AND j.type='identity.resolve_batch' AND j.payload->>'import_id'=i.id::text
                      AND j.status IN ('queued','running')
                """), {"batch": batch_id})
            enqueue(db, "traits.recompute", {"mode": "full", **scope},
                    dedupe_key=f"traits:batch:{batch_id}" if batch_id else f"traits:after-identity:{job_id}")
            db.commit()
            finish_phase(engine, job_id)
        return
    if job_type == "traits.recompute":
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from app.db import engine
        from app.traits.engine import backfill_trait_snapshots, recompute_traits
        from app.jobs.phase_progress import begin_phase, finish_phase

        with Session(engine) as db:
            from app.privacy.locking import privacy_shared
            privacy_shared(db)
            begin_phase(engine, job_id, "traits")
            # Acquire before FOR UPDATE of dirty rows. Waiting for the lock
            # after selecting them can deadlock with a full recompute's cleanup.
            db.execute(text(
                "SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"
            ))
            if payload.get("mode") == "dirty":
                profile_ids = db.execute(text("""
                    SELECT profile_id FROM trait_dirty_profiles
                    WHERE dirtied_at <= clock_timestamp() - interval '10 minutes'
                    ORDER BY dirtied_at, profile_id
                    LIMIT 50000
                    FOR UPDATE SKIP LOCKED
                """)).scalars().all()
                if profile_ids:
                    recompute_traits(db, profile_ids=profile_ids, job_id=job_id)
                    db.execute(text("""
                        DELETE FROM trait_dirty_profiles WHERE profile_id=ANY(:profile_ids)
                    """), {"profile_ids": profile_ids})
            elif payload.get("mode") == "full":
                recompute_traits(db, job_id=job_id)
                backfill_trait_snapshots(db)
                db.execute(text("DELETE FROM trait_dirty_profiles"))
            else:
                raise ValueError("traits.recompute requires mode=dirty or mode=full")
            db.commit()
            finish_phase(engine, job_id)
        # Import only after the trait transaction commits: this avoids coupling
        # dashboard route initialization to the job handler (and cache failures
        # can never invalidate data before a failed recompute).
        from app.dashboards.api import invalidate_cache

        invalidate_cache()
        return
    if job_type == "sleep":
        # Bounded, no-I/O delay for exercising manual worker recovery. Defaults
        # to 60 seconds; shorter values are useful for isolated integration tests.
        seconds = min(max(float(payload.get("seconds", 60)), 0), 60)
        time.sleep(seconds)
        return
    if job_type != "noop":
        raise ValueError("Unknown job type")
    # A bounded delay makes the running state visible without external I/O.
    time.sleep(min(max(float(payload.get("seconds", 0.4)), 0), 30))