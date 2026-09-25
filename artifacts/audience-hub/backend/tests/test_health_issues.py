"""Real PostgreSQL issue fixtures, restricted to disposable benchmark databases."""
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.dashboards.health import attention, health_counts
from app.imports.api import review_rejections


@pytest.fixture
def db():
    if os.getenv("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        pytest.skip("Run in the disposable benchmark harness only")
    from app.db import engine
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(connection, join_transaction_mode="create_savepoint") as session:
            yield session
        transaction.rollback()


def test_open_issues_and_review_lifecycle(db):
    source = db.execute(text("""INSERT INTO sources(key,name,kind)
        VALUES ('health-fixture','Health fixture','crm') RETURNING id""")).scalar_one()
    profile = db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
    user = db.execute(text("""INSERT INTO users(subject,name,email,role)
        VALUES ('health-fixture','Health fixture','health-test@example.org','admin') RETURNING id""")).scalar_one()
    actor = SimpleNamespace(id=user, role="admin")
    imp = db.execute(text("""INSERT INTO imports(source_id,filename,record_type,status,rows_rejected)
        VALUES (:s,'report.csv','contact','completed',12) RETURNING id"""),
        {"s": source}).scalar_one()
    db.execute(text("""INSERT INTO jobs(type,status,payload)
        VALUES ('import.run','failed',jsonb_build_object('import_id',CAST(:i AS bigint))),
               ('noop','queued','{}'), ('noop','completed','{}')"""), {"i": imp})
    block = db.execute(text("""INSERT INTO identifier_blocklist(type,value,reason)
        VALUES ('email','shared@example.org','high_cardinality') RETURNING id""")).scalar_one()
    db.execute(text("""INSERT INTO enrichment_values(profile_id,source_id,attribute_key,license_expires_at)
        SELECT :p,:s,'expiry-'||n,CURRENT_DATE+n FROM unnest(ARRAY[-1,0,60,61]) n"""),
        {"p": profile, "s": source})
    before = health_counts(db)
    assert before == dict(failed_jobs=1, rejected_reports=1, blocklist_reviews=1, expiring_enrichment=2)
    # Large unresolved populations are operational backlog, never issue objects.
    db.execute(text("""INSERT INTO source_records(source_id,external_id,raw_hash)
        SELECT :s,n::text,md5(n::text) FROM generate_series(1,500000) n"""), {"s": source})
    assert health_counts(db) == before
    assert sum(health_counts(db, "viewer").values()) == 2
    assert sum(health_counts(db, "analyst").values()) == 3
    from app.dashboards.api import shell_summary
    assert shell_summary(actor, db)["open_issues"] == 5
    assert shell_summary(SimpleNamespace(role="viewer"), db)["open_issues"] == 2
    assert shell_summary(SimpleNamespace(role="analyst"), db)["open_issues"] == 3
    assert len(attention(db, "admin", 0)) == 4
    review_rejections(imp, actor, db)
    review_rejections(imp, actor, db)  # duplicate audit entries do not inflate totals
    assert health_counts(db)["rejected_reports"] == 0
    assert db.execute(text("SELECT rows_rejected FROM imports WHERE id=:i"), {"i": imp}).scalar_one() == 12
    db.execute(text("UPDATE imports SET rows_rejected=13 WHERE id=:i"), {"i": imp})
    assert health_counts(db)["rejected_reports"] == 1
    review_rejections(imp, actor, db)
    from app.profiles.api import approve_blocklist_item
    approve_blocklist_item(block, actor, db)
    assert health_counts(db)["blocklist_reviews"] == 0
    db.execute(text("UPDATE jobs SET status='cancelled' WHERE status='failed'"))
    db.execute(text("UPDATE enrichment_values SET license_expires_at=CURRENT_DATE+61"))
    assert sum(health_counts(db).values()) == 0
    assert shell_summary(actor, db)["open_issues"] == 0
    assert attention(db, "admin", 0) == []


def test_running_reports_cannot_be_reviewed(db):
    from fastapi import HTTPException
    source = db.execute(text("""INSERT INTO sources(key,name,kind)
        VALUES ('running-health','Health fixture','crm') RETURNING id""")).scalar_one()
    imp = db.execute(text("""INSERT INTO imports(source_id,filename,record_type,status,rows_rejected)
        VALUES (:s,'running.csv','contact','running',3) RETURNING id"""), {"s": source}).scalar_one()
    with pytest.raises(HTTPException) as error:
        review_rejections(imp, SimpleNamespace(id=None), db)
    assert error.value.status_code == 409
    assert health_counts(db)["rejected_reports"] == 0