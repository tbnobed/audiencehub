"""Bulk engine checks run only in the disposable PostgreSQL harness."""
import os
import uuid
import importlib
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine
from app.identity.bulk import resolve_bulk
from app.identity.resolver import _record_identifiers

pytestmark = pytest.mark.skipif(
    os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
    reason="requires disposable benchmark PostgreSQL",
)


@pytest.mark.parametrize("case", [
    "test_resolver_merges_profiles_moves_related_rows_and_preserves_opt_out",
    "test_resolver_batch_is_idempotent_for_resolved_records",
    "test_zeta_enrichment_reference_links_to_crm_and_reimport_is_idempotent",
    "test_component_merging_three_profiles_records_two_merges",
    "test_anonymous_event_backfill_updates_database",
    "test_blocklisted_identifier_never_links_records",
    "test_high_cardinality_identifier_is_auto_blocklisted",
    "test_gift_records_do_not_trigger_high_cardinality_for_donor_identifiers",
])
def test_bulk_preserves_existing_identity_semantics(case, monkeypatch):
    """Run unmodified existing behavioral assertions against the new engine."""
    existing = importlib.import_module("test_identity_resolver")
    monkeypatch.setattr(existing, "resolve_batch",
                        lambda db, limit=500, job_id=None: resolve_bulk(db, job_id))
    getattr(existing, case)()


@pytest.mark.parametrize("imports_active", [False, True])
def test_worker_selects_bulk_only_when_import_queue_is_quiet(monkeypatch, imports_active):
    from app.jobs.handlers import run
    calls = []

    class FakeSession:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def scalar(self, _statement):
            return imports_active

        def commit(self):
            calls.append("commit")

    def chosen(name):
        def resolve(_db, **_kwargs):
            calls.append(name)
            return {"records": 0, "profiles_created": 0, "merges": 0}
        return resolve

    monkeypatch.setattr("sqlalchemy.orm.Session", FakeSession)
    monkeypatch.setattr("app.identity.bulk.resolve_bulk", chosen("bulk"))
    monkeypatch.setattr("app.identity.resolver.resolve_batch", chosen("bounded"))
    monkeypatch.setattr("app.jobs.queue.enqueue", lambda *_args, **_kwargs: None)
    run("identity.resolve_batch", {})
    assert calls == ["bounded" if imports_active else "bulk", "commit", "commit"]


def test_bulk_transitive_pending_and_existing_owner_closure():
    with Session(engine) as db:
        source = db.scalar(text("""
            INSERT INTO sources(key,name,kind,record_types) VALUES(:key,'bulk','csv',ARRAY['contact']) RETURNING id
        """), {"key": uuid.uuid4().hex})
        db.execute(text("""
            INSERT INTO source_records(source_id,external_id,email_norm,phone_e164,raw_hash,attributes)
            VALUES(:s,'a','a@example.org',NULL,'a','{}'),
                  (:s,'b','b@example.org',NULL,'b','{}'),
                  (:s,'c','c@example.org',NULL,'c','{}')
        """), {"s": source})
        assert resolve_bulk(db) == {"records": 3, "profiles_created": 3, "merges": 0}
        ids = dict(db.execute(text("SELECT external_id,profile_id FROM source_records WHERE source_id=:s"),
                              {"s": source}).all())
        # The newest numeric ID is deliberately the oldest person.
        db.execute(text("UPDATE profiles SET first_seen_at='2000-01-01' WHERE id=:id"), {"id": ids["c"]})
        db.execute(text("""
            INSERT INTO identifiers(type,value,profile_id)
            VALUES('phone','+12145550111',:b),('phone','+12145550112',:c)
        """), {"b": ids["b"], "c": ids["c"]})
        db.execute(text("""
            INSERT INTO source_records(source_id,external_id,email_norm,phone_e164,raw_hash,attributes)
            VALUES(:s,'bridge1','a@example.org','+12145550111','d','{}'),
                  (:s,'bridge2','b@example.org','+12145550112','e','{}')
        """), {"s": source})
        assert resolve_bulk(db) == {"records": 2, "profiles_created": 0, "merges": 2}
        assert db.scalars(text("SELECT DISTINCT profile_id FROM source_records WHERE source_id=:s"),
                          {"s": source}).all() == [ids["c"]]
        assert resolve_bulk(db)["records"] == 0
        db.rollback()


def test_bulk_guard_consent_survivorship_and_external_namespace():
    with Session(engine) as db:
        source = db.scalar(text("""
            INSERT INTO sources(key,name,kind,record_types) VALUES(:key,'bulk','csv',ARRAY['contact']) RETURNING id
        """), {"key": uuid.uuid4().hex})
        db.execute(text("""
            INSERT INTO source_records(source_id,external_id,email_norm,raw_hash,attributes)
            SELECT :s,n::text,'shared@example.org',n::text,'{}'::jsonb FROM generate_series(1,26)n
        """), {"s": source})
        assert resolve_bulk(db)["profiles_created"] == 26
        assert db.scalar(text("SELECT reason FROM identifier_blocklist WHERE type='email' AND value='shared@example.org'")) == "high_cardinality"
        db.execute(text("""
            INSERT INTO source_records(source_id,external_id,email_norm,raw_hash,attributes)
            VALUES(:s,'consent1','consent@example.org','c1',
              '{"_import_consent":{"channel":"email","status":"opted_out","captured_at":"2020-01-01"}}'),
            (:s,'consent2','consent@example.org','c2',
              '{"_import_consent":{"channel":"email","status":"opted_in","captured_at":"2025-01-01"}}')
        """), {"s": source})
        assert resolve_bulk(db)["profiles_created"] == 1
        assert db.scalar(text("""
          SELECT c.status FROM consents c JOIN source_records sr ON sr.profile_id=c.profile_id
          WHERE sr.source_id=:s AND sr.external_id='consent1'
        """), {"s": source}) == "opted_out"
        db.rollback()


def test_bulk_external_keys_match_legacy_blank_whitespace_and_json_references():
    """NULL concatenation never manufactures a namespace-only external key."""
    with Session(engine) as db:
        key = f"parity_{uuid.uuid4().hex}"
        source = db.scalar(text("""
            INSERT INTO sources(key,name,kind,record_types)
            VALUES(:key,'parity','csv',ARRAY['contact']) RETURNING id
        """), {"key": key})
        cases = [
            ("", {}),
            (" ", {"_identity_reference": {"source_key": "crm", "contact_external_id": None}}),
            ("\t\n", {"_identity_reference": {"source_key": "crm", "contact_external_id": ""}}),
            ("\u2003", {"_identity_reference": {"source_key": "crm", "contact_external_id": "\t \n"}}),
            (" ordinary ", {"_identity_reference": {"source_key": "\t crm \u2003", "contact_external_id": " \t ref \n"}}),
            ("json-text", {"_identity_reference": {"source_key": "crm", "contact_external_id": '{"id": "nested"}'}}),
            ("invalid-source", {"_identity_reference": {"source_key": {}, "contact_external_id": "ref"}}),
            ("raw-object-text", {"_identity_reference": '{"source_key":"crm","contact_external_id":"ref"}'}),
        ]
        expected = {}
        for external_id, attributes in cases:
            rid = db.scalar(text("""
                INSERT INTO source_records(source_id,external_id,raw_hash,attributes)
                VALUES(:s,:ext,:hash,CAST(:attrs AS jsonb)) RETURNING id
            """), {"s": source, "ext": external_id, "hash": uuid.uuid4().hex,
                   "attrs": json.dumps(attributes)})
            expected[rid] = _record_identifiers(
                {"external_id": external_id, "attributes": attributes}, key
            )
        assert db.scalar(text("SELECT 'crm:' || NULL::text")) is None
        resolve_bulk(db)
        actual = {rid: set() for rid in expected}
        for rid, kind, value in db.execute(text("""
            SELECT record_id,kind,value FROM ir_edges WHERE kind<>'profile'
        """)):
            actual[rid].add((kind, value))
        assert actual == expected
        assert not any(value == f"{key}:" or value == "crm:"
                       for pairs in actual.values() for _, value in pairs)
        db.rollback()


def test_import_keeps_reference_json_as_text_not_nested_identifier_objects():
    from app.imports.validation import map_and_validate_row
    row = map_and_validate_row(
        {"id": "1", "ref": '{"id":"nested"}', "attrs": '{"contact_external_id":{"id":"nested"}}'},
        {"id": "external_id", "ref": "contact_external_id", "attrs": "attributes._identity_reference"},
        "contact",
    )
    assert isinstance(row["values"]["contact_external_id"], str)
    assert isinstance(row["values"]["attributes"]["_identity_reference"], str)


def test_bulk_empty_call_probes_without_building_temporary_graph(monkeypatch):
    module = importlib.import_module("app.identity.bulk")
    original = module._sql
    statements = []

    def recorded(db, statement, **params):
        statements.append(statement)
        return original(db, statement, **params)

    monkeypatch.setattr(module, "_sql", recorded)
    with Session(engine) as db:
        assert resolve_bulk(db) == {"records": 0, "profiles_created": 0, "merges": 0}
        assert len(statements) == 1
        assert "ORDER BY id LIMIT 1" in statements[0]
        assert "CREATE TEMP" not in statements[0]
        db.rollback()