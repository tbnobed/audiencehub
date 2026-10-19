"""Retryable cleanup of registered demo files after an authorized go-live wipe."""
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.config import get_settings


def cleanup_registered_files(engine):
    settings = get_settings()
    roots = [Path(settings.upload_dir).resolve(), Path(settings.export_dir).resolve()]
    with Session(engine) as db:
        paths = list(db.scalars(text("SELECT path FROM operations_cleanup_files ORDER BY path")))
        for raw in paths:
            path = Path(raw)
            resolved = path.resolve()
            if not path.is_absolute() or path.is_symlink() or not any(
                root != resolved and root in resolved.parents for root in roots
            ):
                raise RuntimeError("Demo file cleanup refused an untrusted path; receipt retained for operator review")
            if path.exists() and not path.is_file():
                raise RuntimeError("Demo file cleanup found a non-file; receipt retained")
            path.unlink(missing_ok=True)
            db.execute(text("DELETE FROM operations_cleanup_files WHERE path=:path"), {"path": raw})
        db.commit()
