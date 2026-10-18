"""The public importer owns its budget even without a worker lifecycle."""
import pytest
from sqlalchemy import text
from test_consent_ingestion_pg import db  # isolated, migrated PostgreSQL fixture


@pytest.mark.parametrize("fails", [False, True])
def test_import_timeout_survives_checkpoints_without_leaking(db, monkeypatch, fails):
    from app.imports import service
    from app.config import get_settings

    expected = get_settings().bulk_statement_timeout_seconds * 1000
    original = db.scalar(text("SELECT current_setting('statement_timeout')"))
    seen = []

    def milliseconds():
        return db.scalar(text(
            "SELECT setting::bigint FROM pg_settings WHERE name='statement_timeout'"))

    def importing(_ident, _progress, session):
        assert session is db
        seen.append(milliseconds())  # a caller already opened this transaction
        db.commit()
        seen.append(milliseconds())  # a new checkpoint transaction
        db.rollback()
        seen.append(milliseconds())  # an error/retry transaction
        db.commit()
        if fails:
            raise RuntimeError("synthetic import failure")
        return {"rows_ok": 1}

    monkeypatch.setattr(service, "_run_import", importing)
    if fails:
        with pytest.raises(RuntimeError, match="synthetic"):
            service.run_import(1, db=db)
    else:
        assert service.run_import(1, db=db) == {"rows_ok": 1}
    assert seen == [expected, expected, expected]
    assert db.scalar(text("SHOW statement_timeout")) == original
