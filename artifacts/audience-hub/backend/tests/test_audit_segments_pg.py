"""Adversarial source evidence independent of the large synthetic seed."""
import json
from datetime import date

from sqlalchemy import select, text
from test_consent_ingestion_pg import db

from app.models import Segment
from app.segments.compiler import compile_definition
from app.segments.service import ensure_templates
from app.segments.templates import legacy_template_definitions, template_definitions


def test_untouched_six_defaults_become_23(db):
    ensure_templates(db, legacy_template_definitions())
    assert len(list(db.scalars(select(Segment)))) == 6
    ensure_templates(db)
    assert len(list(db.scalars(select(Segment)))) == 23
    assert len(list(db.scalars(select(Segment).where(Segment.template_key.is_not(None))))) == 23


def test_retiring_generic_catalog_preserves_saved_audiences(db):
    ensure_templates(db, legacy_template_definitions())
    saved = list(db.scalars(select(Segment)))
    saved[0].name = "An edited saved audience"
    # Saved/activated legacy segments must not be repurposed.
    for segment in saved:
        segment.status = "active"
    definitions = [s.definition for s in saved]
    ensure_templates(db)
    assert len(list(db.scalars(select(Segment).where(Segment.template_key.is_not(None))))) == 23
    assert len(list(db.scalars(select(Segment)))) == 29
    assert saved[0].name == "An edited saved audience"
    assert [s.definition for s in saved] == definitions
    assert all(s.template_key is None for s in saved)
    ensure_templates(db)
    assert len(list(db.scalars(select(Segment)))) == 29


def test_audit_source_evidence_null_boolean_imported_models_and_events(db):
    db.execute(text("INSERT INTO sources(id,key,name,kind) OVERRIDING SYSTEM VALUE VALUES(1,'audit','Audit','csv')"))
    db.execute(text("INSERT INTO source_codes(key,name) VALUES('T4XWDGT','Giving Tuesday 2024')"))
    for ident, attrs in enumerate([
        {"tbn_plus_subscription": "basic", "has_recurring_donation": True, "rfm_raw_frequency": 1,
         "rfm_recency_score": 0, "donor_account_number": "   ", "rfm_date": ""},
        {"tbn_plus_subscription": "basic", "has_recurring_donation": False, "rfm_raw_frequency": 2,
         "rfm_recency_score": 1, "donor_account_number": "123", "rfm_date": "2026-10-16",
         "donor_account_status": "active"},
        {"tbn_plus_subscription": "basic", "has_recurring_donation": None,
         "donor_account_number": "", "tbn_video_on_demand_updates_opt_status": False},
    ], start=1):
        db.execute(text("INSERT INTO profiles(id) OVERRIDING SYSTEM VALUE VALUES(:id)"),
                   {"id": ident})
        db.execute(text("""INSERT INTO source_records(source_id,external_id,profile_id,attributes,raw_hash)
            VALUES(1,:external,:id,CAST(:attrs AS jsonb),'audit-fixture-' || :external)"""),
                   {"external": str(ident), "id": ident, "attrs": json.dumps(attrs)})
    db.execute(text("""INSERT INTO events(source_id,message_id,profile_id,type,name,occurred_at,properties)
        VALUES(1,'d1',1,'track','donation','2024-12-03 12:00+00','{"source_code":"T4XWDGT"}'),
              (1,'d2',2,'track','donation','2024-12-04 12:00+00','{"source_code":"T4XWDGT"}'),
              (1,'v1',2,'track','vod_viewed','2026-10-16 12:00+00','{}'),
              (1,'v2',3,'track','Video Watched','2026-10-16 12:00+00','{}')"""))
    # Imported canonical properties are now projected by the traits pipeline.
    db.execute(text("INSERT INTO profile_traits(profile_id) SELECT id FROM profiles"))
    from app.segments.projection import refresh_projections
    refresh_projections(db)
    definitions = {t["name"]: t["definition"] for t in template_definitions()}
    def ids(name):
        return set(db.scalars(compile_definition(db, definitions[name], date(2026, 10, 17))))
    assert ids("Basic Recurring Donors") == {1}
    assert ids("Basic One-Time Donors") == {1}
    assert ids("RFM Lapsed Donors") == {1}
    assert ids("RFM TBN All Donor Reference") == {2}
    assert ids("TBN App Non-Donors") == {3}
    assert ids("VOD Optouts") == {3}
    assert ids("Giving Tuesday 2024") == {1}
    assert ids("Customers with VOD Viewed") == {2}
