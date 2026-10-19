"""Saved partner audience correctness against private migrated PostgreSQL."""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import text, select
from fastapi import HTTPException
from test_consent_ingestion_pg import db
from app.models import Segment
from app.segments import api
from app.segments.service import materialize, ensure_templates, validate_references

AS_OF = datetime(2024,12,31,tzinfo=timezone.utc)


def definition(region="TX"):
    return {"version":1,"root":{"op":"and","rules":[
        {"type":"profile","field":"region","operator":"eq","value":region}]}}


def setup(db):
    db.execute(text("""INSERT INTO users(id,subject,email,name,role)
      OVERRIDING SYSTEM VALUE VALUES(1,'segments-test','test@example.test','Analyst','analyst')"""))
    db.execute(text("""INSERT INTO profiles(id,region,email,phone)
      OVERRIDING SYSTEM VALUE VALUES(1,'TX','partner@example.test','+12145551234'),
      (2,'CA','other@example.test',NULL),(3,'TX',NULL,NULL)"""))
    return SimpleNamespace(id=1,role="analyst")


def test_templates_idempotent_and_preserve_edits(db):
    ensure_templates(db)
    templates = list(db.scalars(select(Segment)))
    assert len(templates) == 23
    templates[0].name = "My editable partners"
    db.flush()
    ensure_templates(db)
    assert len(list(db.scalars(select(Segment)))) == 23
    assert templates[0].name == "My editable partners"


def test_audited_templates_owned_active_daily_and_startup_preserves_pause(db):
    setup(db)
    db.execute(text("UPDATE users SET role='admin' WHERE id=1"))
    ensure_templates(db)
    rows = list(db.scalars(select(Segment)))
    assert len(rows) == 23
    assert all(row.status == "active" and row.created_by == 1
               and row.refresh_schedule == "daily" for row in rows)
    rows[0].status = "draft"
    db.flush()
    ensure_templates(db)
    assert rows[0].status == "draft"


def test_activate_all_templates_rerun_dedupes_pending_refreshes(db):
    from app.segments.service import activate_templates
    setup(db)
    db.execute(text("UPDATE users SET role='admin' WHERE id=1"))
    first = activate_templates(db)
    second = activate_templates(db)
    assert len(first["ids"]) == 23
    assert first["job_ids"] == second["job_ids"]
    assert db.scalar(text("SELECT count(*) FROM jobs WHERE type='segment.materialize'")) == 23


def test_bulk_pause_refresh_activate_retains_counts_and_checks_dependencies(db):
    from app.segments.service import bulk_action
    user = setup(db)
    first = api.create(api.Write(name="Base", definition=definition(), status="active"), user, db)
    ref = {"version": 1, "root": {"op": "and", "rules": [
        {"type": "segment", "segment_id": first["id"], "operator": "in"}]}}
    second = api.create(api.Write(name="Dependent", definition=ref, status="active"), user, db)
    materialize(db, second["id"], AS_OF)
    with pytest.raises(ValueError, match="references"):
        bulk_action(db, [first["id"]], "pause", user.id)
    ids = [first["id"], second["id"]]
    bulk_action(db, ids, "pause", user.id)
    assert all(db.get(Segment, key).status == "draft" for key in ids)
    assert all(db.get(Segment, key).last_count == 2 for key in ids)
    with pytest.raises(ValueError, match="Activate"):
        bulk_action(db, ids, "refresh", user.id)
    bulk_action(db, ids, "activate", user.id)
    first_jobs = bulk_action(db, ids, "refresh", user.id)["job_ids"]
    assert bulk_action(db, ids, "refresh", user.id)["job_ids"] == first_jobs


def test_seed_finalizer_populates_count_change_and_refreshed_at(db):
    from app.segments.service import materialize_seed_templates
    setup(db)
    db.execute(text("UPDATE users SET role='admin' WHERE id=1"))
    rows = materialize_seed_templates(db, AS_OF)
    assert len(rows) == 23
    assert all(row.last_count is not None and row.last_materialized_at is not None for row in rows)
    assert all(api.serialize(db, row)["change"] is not None for row in rows)


def test_save_materialize_changes_and_history(db):
    user = setup(db)
    row = api.create(api.Write(name="TX partners",definition=definition(),status="active"), user, db)
    assert db.scalar(text("SELECT count(*) FROM jobs WHERE type='segment.materialize'")) == 1
    assert materialize(db,row["id"],AS_OF) == 2
    db.execute(text("UPDATE profiles SET region='CA' WHERE id=1"))
    db.execute(text("UPDATE profiles SET region='TX' WHERE id=2"))
    assert materialize(db,row["id"],AS_OF) == 2
    history = api.history(row["id"],user,db)["items"]
    assert (history[0]["added"],history[0]["removed"]) == (1,1)
    assert (history[1]["added"],history[1]["removed"]) == (2,0)
    assert len(api.members(row["id"],"",user,db)["items"]) == 2


def test_dependencies_cycles_archive_and_rollback(db):
    user = setup(db)
    first = api.create(api.Write(name="First partners",definition=definition(),status="active"),user,db)
    ref = {"version":1,"root":{"op":"and","rules":[{"type":"segment","segment_id":first["id"],"operator":"in"}]}}
    second = api.create(api.Write(name="Second partners",definition=ref,status="active"),user,db)
    assert materialize(db,second["id"],AS_OF) == 2
    assert db.get(Segment,first["id"]).last_count == 2
    cyclic = {"version":1,"root":{"op":"and","rules":[{"type":"segment","segment_id":second["id"],"operator":"in"}]}}
    with pytest.raises(ValueError,match="cycle"):
        validate_references(db,first["id"],cyclic,"active")
    with pytest.raises(HTTPException) as error:
        api.archive(first["id"],user,db)
    assert error.value.status_code == 409
    assert db.get(Segment,first["id"]).status == "active"


def test_preview_sample_masking_and_validation(db):
    user = setup(db)
    body = api.Preview(definition=definition(),as_of=AS_OF)
    assert api.preview(body,user,db)["count"] == 2
    sample = api.sample(body,SimpleNamespace(role="viewer"),db)
    assert sample["items"][0]["email"] != "partner@example.test"
    assert sample["items"][0]["phone"] != "+12145551234"
    assert sample["breakdowns"] == {"status": [], "source": []}
    breakdowns = api.breakdowns(body, SimpleNamespace(role="viewer"), db)
    assert sum(r["count"] for r in breakdowns["breakdowns"]["status"]) == 2
    with pytest.raises(HTTPException) as error:
        api.preview(api.Preview(definition={"version":1,"root":{"op":"and","rules":[
            {"type":"profile","field":"not_a_column","operator":"eq","value":"anything"}]}}),user,db)
    assert error.value.status_code == 422


def test_http_roles_and_activity(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth.deps import current_user
    from app.db import session_scope
    user = setup(db)
    row = api.create(api.Write(name="Saved partners",definition=definition(),status="active"),user,db)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[session_scope] = lambda: db
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1,role="viewer")
    client = TestClient(app)
    assert client.get("/api/segments/fields").status_code == 200
    assert client.get(f"/api/segments/{row['id']}/activity").json()["activations"] == []
    assert client.get(f"/api/segments/{row['id']}/members").status_code == 403
    assert client.post("/api/segments",json={"name":"No","definition":definition()}).status_code == 403
    assert client.post("/api/segments/explain",json={"definition":definition()}).status_code == 403
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1,role="admin")
    response = client.post("/api/segments/explain",json={"definition":definition()})
    assert response.status_code == 200, response.text


def test_materialization_rollback_and_active_dependency_guard(db):
    user = setup(db)
    first = api.create(api.Write(name="Base partners",definition=definition(),status="active"),user,db)
    ref = {"version":1,"root":{"op":"and","rules":[{"type":"segment","segment_id":first["id"],"operator":"in"}]}}
    second = api.create(api.Write(name="Related partners",definition=ref,status="active"),user,db)
    with pytest.raises(HTTPException) as error:
        api.update(first["id"],api.Write(name="Base partners",definition=definition(),status="draft"),user,db)
    assert error.value.status_code == 409
    materialize(db,second["id"],AS_OF)
    db.rollback()
    assert db.scalar(text("SELECT count(*) FROM segment_membership")) == 0
    assert db.scalar(text("SELECT count(*) FROM segment_counts")) == 0


def test_queued_handler_dependencies_and_traits_deferral(db, monkeypatch):
    from app.jobs import handlers
    from app.models import Job
    from app.identity.locking import IdentityResolutionDeferred
    import sqlalchemy.orm
    user = setup(db)
    first = api.create(api.Write(name="Base queued partners",definition=definition(),status="active"),user,db)
    ref = {"version":1,"root":{"op":"and","rules":[{"type":"segment","segment_id":first["id"],"operator":"in"}]}}
    second = api.create(api.Write(name="Dependent queued partners",definition=ref,status="active"),user,db)
    class BorrowSession:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return db
        def __exit__(self, *args): pass
    monkeypatch.setattr(sqlalchemy.orm,"Session",BorrowSession)
    job = db.scalars(select(Job).where(Job.type=="segment.materialize").order_by(Job.id.desc())).first()
    assert job.payload["segment_id"] == second["id"]
    handlers.run(job.type,job.payload,job.id)
    assert db.get(Segment,first["id"]).last_count == 2
    assert db.get(Segment,second["id"]).last_count == 2
    from app.jobs.queue import enqueue
    enqueue(db,"traits.recompute",{"mode":"full"})
    db.flush()
    with pytest.raises(IdentityResolutionDeferred):
        handlers.run(job.type,job.payload,job.id)


def test_active_definition_change_does_not_dedupe_against_old_running_job(db):
    from app.models import Job
    user = setup(db)
    row = api.create(api.Write(name="Changing partners",definition=definition(),status="active"),user,db)
    first_job = db.scalars(select(Job).where(Job.type=="segment.materialize")).first()
    first_job.status = "running"
    db.commit()
    api.update(row["id"],api.Write(name="Changing partners",definition=definition("CA"),status="active"),user,db)
    jobs = list(db.scalars(select(Job).where(Job.type=="segment.materialize").order_by(Job.id)))
    assert len(jobs) == 2
    assert jobs[0].dedupe_key != jobs[1].dedupe_key
    assert jobs[1].status == "queued"


@pytest.mark.parametrize("op,negate", [("and",False),("or",False),("and",True),("or",True)])
def test_delta_prunes_empty_ancestors_but_saved_groups_stay_strict(db, op, negate):
    user = setup(db)
    leaf = definition()["root"]["rules"][0]
    nested = {"op":op,"negate":negate,"rules":[{"op":"or","rules":[leaf]}]}
    original = {"version":1,"root":{"op":"and","rules":[nested,
        {"type":"profile","field":"region","operator":"eq","value":"CA"}]}}
    result = api.delta(api.Delta(definition=original,path="root.rules[0].rules[0].rules[0]"),user,db)
    assert result["without_count"] == 1
    solo = {"version":1,"root":{"op":op,"negate":negate,"rules":[leaf]}}
    result = api.delta(api.Delta(definition=solo,path="root.rules[0]"),user,db)
    assert result["without_count"] == 3
    assert len(original["root"]["rules"][0]["rules"][0]["rules"]) == 1
    with pytest.raises(HTTPException):
        api.create(api.Write(name="Empty partners",definition={"version":1,"root":{"op":op,"rules":[]}}),user,db)


def test_typeahead_filters_before_limit_and_escapes_literals(db):
    user = setup(db)
    db.execute(text("INSERT INTO sources(id,key,name,kind) OVERRIDING SYSTEM VALUE VALUES(1,'test','Test','csv')"))
    db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,campaign)
      SELECT 1,n::text,1,1,CURRENT_DATE,'campaign-'||lpad(n::text,3,'0') FROM generate_series(1,250) n"""))
    db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,campaign)
      VALUES(1,'literal',1,1,CURRENT_DATE,'literal%_campaign')"""))
    assert len(api.typeahead("campaign","",user,db)["items"]) == 200
    assert api.typeahead("gift.campaign","250",user,db)["items"] == ["campaign-250"]
    assert api.typeahead("campaign","%_",user,db)["items"] == ["literal%_campaign"]


def test_schedule_normalizes_only_known_legacy_cron():
    from pydantic import ValidationError
    assert api.Write(name="Partners",definition=definition(),refresh_schedule="0 4 * * *").refresh_schedule == "daily"
    with pytest.raises(ValidationError):
        api.Write(name="Partners",definition=definition(),refresh_schedule="* * * * *")
    from app.jobs.handlers import SegmentTraitsRefreshRequired
    from app.logging_config import safe_job_error
    message = safe_job_error(SegmentTraitsRefreshRequired("secret@example.test"))
    assert "repair traits" in message
    assert "secret@example.test" not in message


def test_nightly_success_barrier_and_shared_trait_lock(db, monkeypatch):
    from app.jobs import handlers
    from app.jobs.queue import enqueue
    from app.models import ScheduledRun
    from app.identity.locking import IdentityResolutionDeferred
    import sqlalchemy.orm
    user = setup(db)
    row = api.create(api.Write(name="Scheduled partners",definition=definition(),status="active"),user,db)
    nightly = enqueue(db,"traits.recompute",{"mode":"full"})
    db.add(ScheduledRun(task="traits.nightly_full",window_key="2024-12-31",job_id=nightly.id))
    db.commit()
    class BorrowSession:
        def __init__(self,*args,**kwargs): pass
        def __enter__(self): return db
        def __exit__(self,*args): pass
    monkeypatch.setattr(sqlalchemy.orm,"Session",BorrowSession)
    payload = {"segment_id":row["id"],"traits_window":"2024-12-31"}
    for status in ("queued","running"):
        nightly.status = status
        db.commit()
        with pytest.raises(IdentityResolutionDeferred):
            handlers.run("segment.materialize",payload)
        db.rollback()
    for status in ("failed","cancelled"):
        nightly.status = status
        db.commit()
        with pytest.raises(ValueError,match="did not succeed"):
            handlers.run("segment.materialize",payload)
        db.rollback()
        with pytest.raises(ValueError,match="Latest full traits"):
            handlers.run("segment.materialize",{"segment_id":row["id"]})
        db.rollback()
    nightly.status = "succeeded"
    db.commit()
    with db.get_bind().connect() as blocker:
        blocker.execute(text("SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"))
        with pytest.raises(IdentityResolutionDeferred):
            handlers.run("segment.materialize",payload)
        db.rollback()
        blocker.rollback()
    handlers.run("segment.materialize",payload)
    assert db.get(Segment,row["id"]).last_count == 2
    with pytest.raises(ValueError,match="did not succeed"):
        handlers.run("segment.materialize",dict(payload,traits_window="2024-12-30"))


def test_history_export_role_csv_and_persisted_audit(db):
    import csv
    import io
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth.deps import current_user
    from app.db import session_scope
    user = setup(db)
    row = api.create(api.Write(name="=Unsafe filename/partners",definition=definition(),status="active"),user,db)
    materialize(db,row["id"],AS_OF)
    db.commit()
    expected = api.history(row["id"],user,db)["items"]
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[session_scope] = lambda: db
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1,role="viewer")
    client = TestClient(app)
    path = f"/api/segments/{row['id']}/count-history/export"
    assert client.post(path,json={}).status_code == 403
    assert db.scalar(text("SELECT count(*) FROM audit_log WHERE action='segment.count_history.export'")) == 0
    app.dependency_overrides[current_user] = lambda: user
    response = client.post(path,json={})
    assert response.status_code == 200
    result = response.json()
    assert result["filename"] == f"partner-audience-{row['id']}-count-history.csv"
    actual = list(csv.reader(io.StringIO(result["csv"])))
    assert actual == [["counted_at","count","added","removed"]] + [
        [r["counted_at"].isoformat(),str(r["count"]),str(r["added"]),str(r["removed"])] for r in expected]
    db.rollback()
    audit = db.execute(text("""SELECT user_id,entity_type,entity_id,details FROM audit_log
      WHERE action='segment.count_history.export'""")).mappings().one()
    assert (audit["user_id"],audit["entity_type"],audit["entity_id"],audit["details"]) == (
        1,"segment",str(row["id"]),{"rows":len(expected)})