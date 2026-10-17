from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import engine
from app.privacy.service import execute_deletion, expire_import_uploads
from app.identity.locking import IdentityResolutionDeferred


def run_upload_retention():
    """Call from the existing retention dispatch, before opening its transaction."""
    with Session(engine) as db:
        result = expire_import_uploads(db)
        db.commit()
        return result


def run_deletion(request_id, job_id=None):
    try:
        with Session(engine) as db:
            execute_deletion(db, request_id)
            db.commit()
    except IdentityResolutionDeferred:
        raise
    except Exception as exc:
        safe_errors = {
            "Unreviewed partner reference in database schema; privacy execution refused.",
            "Privacy file revocation refused an unsafe stored path.",
            "Deletion requires approval before execution.",
            "Deletion requires two distinct admins.",
            "The approving admin is no longer active.",
            "This legacy privacy request must be recreated before safe execution.",
            "Privacy request could not be verified with the configured encryption key.",
            "Privacy request could not be verified with the configured suppression key.",
        }
        message = ("Cannot revoke stored files; verify upload/export storage permissions and retry."
                   if isinstance(exc, OSError) else
                   str(exc) if isinstance(exc, ValueError) and str(exc) in safe_errors else
                   "Privacy execution failed safely; review schema, storage and approval state before retry.")
        with Session(engine) as db:
            db.execute(text("""UPDATE deletion_requests SET status='failed',
              error=:message WHERE id=:id AND status IN ('approved','failed')"""),
              {"id": request_id, "message": message})
            db.commit()
        # Do not expose identifiers, SQL parameters, filenames, or arbitrary exception text.
        raise RuntimeError("Privacy execution failed; inspect the private server diagnostic code.") from None
