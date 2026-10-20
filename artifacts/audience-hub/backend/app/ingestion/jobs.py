from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import engine
from app.identity.bulk import resolve_bulk
from app.identity.locking import acquire_resolver_lock, IdentityResolutionDeferred
from app.ingestion.service import backfill


def resolve(db, source_id, job_id=None):
    acquire_resolver_lock(db)
    if db.scalar(text("""SELECT EXISTS(SELECT 1 FROM jobs WHERE type IN ('import.run','identity.resolve_batch')
      AND status IN ('queued','running')) OR EXISTS(SELECT 1 FROM imports i
      WHERE status IN ('failed','cancelled') AND EXISTS(SELECT 1 FROM source_records sr
        WHERE sr.last_import_id=i.id AND sr.resolved_at IS NULL))""")):
        raise IdentityResolutionDeferred("Ingestion identity waits for the import barrier.")
    resolve_bulk(db, job_id=job_id, mode="incremental")
    backfill(db, source_id)
    from app.meta_capture import finish_consents
    finish_consents(db, source_id)


def run_resolution(source_id, job_id=None):
    with Session(engine) as db:
        resolve(db, source_id, job_id)
        db.commit()
