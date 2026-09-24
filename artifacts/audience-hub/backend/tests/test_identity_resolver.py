from datetime import datetime, timezone
import uuid

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.db import engine
from app.importer import _mapping
from app.identity.resolver import UnionFind, backfill_anonymous_events, resolve_batch
from app.identity.locking import (
    IDENTIFIER_LOCK_BUCKETS, IDENTIFIER_LOCK_NAMESPACE, acquire_identifier_locks,
)
from app.identity.survivorship import select_survivorship_values
from app.imports.service import _row_payload, _upsert_source_records
from app.imports.validation import map_and_validate_row


def test_union_find_connects_transitive_components():
    groups = UnionFind()
    groups.union("a", "b")
    groups.union("b", "c")
    assert groups.find("a") == groups.find("c")
    assert groups.find("d") != groups.find("a")


def test_survivorship_priority_nonempty_and_address_block():
    now = datetime.now(timezone.utc)
    records = [
        {"id": 1, "priority": 20, "updated_at": now, "email_norm": "later@example.org",
         "phone_e164": None, "first_name": "Jane", "last_name": "Doe",
         "address1": "1 High St", "city": "London", "region": None,
         "postal_code": "SW1", "country": "GB"},
        {"id": 2, "priority": 1, "updated_at": now, "email_norm": "",
         "phone_e164": "+12147483647", "first_name": "JANE", "last_name": "DOE",
         "address1": "2 Main St", "city": None, "region": "TX",
         "postal_code": "75001", "country": "US"},
    ]
    values = select_survivorship_values(records)
    assert values["email"] == "later@example.org"
    assert values["phone"] == "+12147483647"
    assert (values["address1"], values["city"], values["country"]) == (
        "2 Main St", None, "US"
    )
    assert (values["first_name"], values["last_name"]) == ("JANE", "DOE")


class _Result:
    rowcount = 2


class _Recorder:
    def __init__(self):
        self.statement = ""
        self.params = {}

    def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params
        return _Result()


def test_anonymous_event_backfill_is_scoped_and_only_fills_unlinked():
    db = _Recorder()
    assert backfill_anonymous_events(db, 7, "anon-1", 42) == 2
    assert "profile_id IS NULL" in db.statement
    assert db.params == {
        "profile_id": 42, "source_id": 7, "anonymous_id": "anon-1"
    }


def test_identifier_lock_buckets_are_bounded_distinct_and_numerically_ordered():
    db = _Recorder()
    acquire_identifier_locks(db, [("email", "b@example.org"), ("external", "a:1"),
                                 ("email", "b@example.org")])
    assert db.params["keys"] == ["email:b@example.org", "external:a:1"]
    assert db.params["buckets"] == IDENTIFIER_LOCK_BUCKETS == 1024
    assert db.params["namespace"] == IDENTIFIER_LOCK_NAMESPACE
    assert "SELECT DISTINCT" in db.statement
    assert "ORDER BY bucket" in db.statement
    assert "((hashtext(lock_key) % :buckets) + :buckets) % :buckets" in db.statement
    assert "pg_advisory_xact_lock(CAST(:namespace AS integer), bucket)" in db.statement


def test_empty_identifier_group_takes_no_bucket_locks():
    db = _Recorder()
    acquire_identifier_locks(db, [])
    assert db.statement == ""


def test_identity_handler_commits_underfull_groups_and_only_stops_at_empty(monkeypatch):
    from app.jobs.handlers import run

    calls = []

    class BatchSession:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def commit(self):
            calls.append("commit")

    sizes = iter([2, 1, 0])

    def resolve(_db, limit, job_id):
        assert limit == 10_000
        assert job_id == 42
        size = next(sizes)
        calls.append(size)
        return {"records": size, "profiles_created": size, "merges": 0}

    monkeypatch.setattr("sqlalchemy.orm.Session", BatchSession)
    monkeypatch.setattr("app.identity.resolver.resolve_batch", resolve)
    run("identity.resolve_batch", {"limit": 10_000}, job_id=42)
    assert calls == [2, "commit", 1, "commit", 0, "commit"]


def test_large_connected_component_resumes_in_bounded_groups():
    """Rollback-only correctness test; isolated scale test covers real commits."""
    prefix = f"resolver_large_component_{uuid.uuid4().hex}"
    with Session(engine) as db:
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['gift']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        import_id = db.execute(text("""
            INSERT INTO imports (source_id, filename, record_type)
            VALUES (:source_id, 'large.csv', 'gift') RETURNING id
        """), {"source_id": source_id}).scalar_one()
        db.execute(text("""
            INSERT INTO source_records
              (source_id, external_id, email_norm, raw_hash, last_import_id)
            SELECT :source_id, :prefix || '-' || n, :email, 'row-' || n, :import_id
            FROM generate_series(1, 1001) n
        """), {"source_id": source_id, "prefix": prefix, "import_id": import_id,
               "email": f"{prefix}@example.org"})
        # The oldest pre-existing profile is encountered only in the final
        # prefix. Earlier commits must not pin the eventual winner incorrectly.
        winner = db.execute(text("""
            INSERT INTO profiles (first_seen_at, last_seen_at)
            VALUES ('2020-01-01', '2020-01-01') RETURNING id
        """)).scalar_one()
        db.execute(text("""
            INSERT INTO identifiers (type, value, profile_id)
            VALUES ('external', :value, :winner)
        """), {"value": f"{prefix}:{prefix}-1001", "winner": winner})
        results = [resolve_batch(db, limit=50_000) for _ in range(3)]
        assert [result["records"] for result in results] == [500, 500, 1]
        assert sum(result["profiles_created"] for result in results) == 1
        assert sum(result["merges"] for result in results) == 1
        assert resolve_batch(db)["records"] == 0
        assert db.scalar(text("""
            SELECT count(DISTINCT profile_id) FROM source_records WHERE source_id=:id
        """), {"id": source_id}) == 1
        assert db.scalar(text("""
            SELECT count(*) FROM source_records
            WHERE source_id=:id AND resolved_at IS NOT NULL
        """), {"id": source_id}) == 1001
        assert db.scalar(text("""
            SELECT count(*) FROM source_records
            WHERE source_id=:id AND profile_id=:winner
        """), {"id": source_id, "winner": winner}) == 1001
        assert db.scalar(text("""
            SELECT count(*) FROM identifiers
            WHERE profile_id=:winner AND (
                (type='external' AND value LIKE :prefix) OR
                (type='email' AND value=:email))
        """), {"winner": winner, "prefix": f"{prefix}:%",
               "email": f"{prefix}@example.org"}) == 1002
        db.rollback()


def test_component_boundary_can_return_underfull_group_without_finishing():
    prefix = f"resolver_boundary_{uuid.uuid4().hex}"
    with Session(engine) as db:
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['contact']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        db.execute(text("""
            INSERT INTO source_records (source_id, external_id, email_norm, raw_hash)
            SELECT :source_id, :prefix || '-' || n,
                   :prefix || '-' || ((n-1)/2) || '@example.org', 'row-' || n
            FROM generate_series(1, 6) n
        """), {"source_id": source_id, "prefix": prefix})
        results = [resolve_batch(db, limit=3) for _ in range(3)]
        assert [result["records"] for result in results] == [2, 2, 2]
        assert sum(result["profiles_created"] for result in results) == 3
        assert resolve_batch(db)["records"] == 0
        db.rollback()


def test_resolver_merges_profiles_moves_related_rows_and_preserves_opt_out():
    """Exercise real PostgreSQL constraints in a rollback-only transaction."""
    prefix = f"resolver_{uuid.uuid4().hex}"
    with Session(engine) as db:
        db.begin()
        source_ids = []
        for suffix in ("a", "b"):
            source_ids.append(db.execute(
                text("""
                    INSERT INTO sources (key, name, kind, record_types, priority, is_active)
                    VALUES (:key, :key, 'csv', ARRAY['contact']::text[], 10, true)
                    RETURNING id
                """),
                {"key": f"{prefix}_{suffix}"},
            ).scalar_one())
        winner = db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
        loser = db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
        db.execute(text("""
            INSERT INTO identifiers (type, value, profile_id)
            VALUES ('email', :email, :winner), ('phone', :phone, :loser)
        """), {"email": f"{prefix}@example.org", "phone": "+12147483647",
               "winner": winner, "loser": loser})
        db.execute(text("""
            INSERT INTO source_records
              (source_id, external_id, email_norm, phone_e164, raw_hash)
            VALUES (:source_id, :external_id, :email, :phone, 'test')
        """), {"source_id": source_ids[0], "external_id": f"{prefix}_bridge",
               "email": f"{prefix}@example.org", "phone": "+12147483647"})
        db.execute(text("""
            INSERT INTO gifts (source_id, external_id, profile_id, amount)
            VALUES (:source_id, :external_id, :loser, 10)
        """), {"source_id": source_ids[0], "external_id": f"{prefix}_gift", "loser": loser})
        occurred_at = datetime.now(timezone.utc)
        db.execute(text("""
            INSERT INTO events (source_id, type, name, occurred_at, profile_id)
            VALUES (:source_id, 'track', 'test', :occurred_at, :loser)
        """), {"source_id": source_ids[0], "occurred_at": occurred_at, "loser": loser})
        db.execute(text("""
            INSERT INTO consents (profile_id, channel, status, source_id)
            VALUES (:winner, 'email', 'opted_in', :source_a),
                   (:loser, 'email', 'opted_out', :source_b)
        """), {"winner": winner, "loser": loser, "source_a": source_ids[0],
               "source_b": source_ids[1]})
        db.execute(text("""
            INSERT INTO enrichment_values
              (profile_id, source_id, attribute_key, value_text, imported_at)
            VALUES (:winner, :source_id, 'appeal', 'older', '2024-01-01T00:00:00Z'),
                   (:loser, :source_id, 'appeal', 'newer', '2024-02-01T00:00:00Z')
        """), {"winner": winner, "loser": loser, "source_id": source_ids[0]})
        segment_id = db.execute(text("""
            INSERT INTO segments (name) VALUES (:name) RETURNING id
        """), {"name": f"{prefix}_segment"}).scalar_one()
        db.execute(text("""
            INSERT INTO segment_membership (segment_id, profile_id) VALUES (:segment_id, :loser)
        """), {"segment_id": segment_id, "loser": loser})
        result = resolve_batch(db)
        assert result["merges"] == 1
        assert db.execute(text(
            "SELECT merged_into_id FROM profiles WHERE id=:loser"
        ), {"loser": loser}).scalar_one() == winner
        assert db.execute(text(
            "SELECT count(*) FROM identifiers WHERE profile_id=:loser"
        ), {"loser": loser}).scalar_one() == 0
        assert db.execute(text(
            "SELECT count(*) FROM profiles WHERE id=:loser AND merged_into_id IS NULL AND NOT is_deleted"
        ), {"loser": loser}).scalar_one() == 0
        assert db.execute(text(
            "SELECT profile_id FROM source_records WHERE external_id=:external_id"
        ), {"external_id": f"{prefix}_bridge"}).scalar_one() == winner
        assert db.execute(text(
            "SELECT profile_id FROM gifts WHERE external_id=:external_id"
        ), {"external_id": f"{prefix}_gift"}).scalar_one() == winner
        assert db.execute(text(
            "SELECT profile_id FROM events WHERE name='test' AND occurred_at=:occurred_at"
        ), {"occurred_at": occurred_at}).scalar_one() == winner
        assert db.execute(text(
            "SELECT status FROM consents WHERE profile_id=:profile_id AND channel='email'"
        ), {"profile_id": winner}).scalar_one() == "opted_out"
        assert db.execute(text("""
            SELECT value_text FROM enrichment_values
            WHERE profile_id=:profile_id AND source_id=:source_id AND attribute_key='appeal'
        """), {"profile_id": winner, "source_id": source_ids[0]}).scalar_one() == "newer"
        assert db.execute(text("""
            SELECT count(*) FROM segment_membership WHERE segment_id=:segment_id AND profile_id=:loser
        """), {"segment_id": segment_id, "loser": loser}).scalar_one() == 0
        assert db.execute(text(
            "SELECT count(*) FROM profile_merges WHERE loser_id=:loser"
        ), {"loser": loser}).scalar_one() == 1
        db.rollback()


def test_resolver_batch_is_idempotent_for_resolved_records():
    prefix = f"resolver_idempotent_{uuid.uuid4().hex}"
    with Session(engine) as db:
        db.begin()
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['contact']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        for suffix in ("1", "2"):
            db.execute(text("""
                INSERT INTO source_records
                  (source_id, external_id, email_norm, raw_hash)
                VALUES (:source_id, :external_id, :email, :external_id)
            """), {"source_id": source_id, "external_id": f"{prefix}_{suffix}",
                   "email": f"{prefix}@example.org"})
        first = resolve_batch(db)
        again = resolve_batch(db)
        assert first["records"] == 2
        assert first["profiles_created"] == 1
        assert again == {"records": 0, "profiles_created": 0, "merges": 0}
        assert db.execute(text("""
            SELECT count(DISTINCT profile_id) FROM source_records WHERE source_id=:source_id
        """), {"source_id": source_id}).scalar_one() == 1
        db.rollback()


def test_resolver_sql_statement_count_is_bounded_by_batch_not_record_count():
    prefix = f"resolver_statement_count_{uuid.uuid4().hex}"
    with Session(engine) as db:
        db.begin()
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['contact']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        db.execute(text("""
            INSERT INTO source_records (source_id, external_id, email_norm, raw_hash)
            SELECT :source_id, :prefix || '-' || n,
                   :prefix || '-' || n || '@example.org', :prefix || '-' || n
            FROM generate_series(1, 200) n
        """), {"source_id": source_id, "prefix": prefix})
        statements = [0]
        connection = db.connection()

        def count_statement(*_args):
            statements[0] += 1

        event.listen(connection, "before_cursor_execute", count_statement)
        try:
            result = resolve_batch(db, limit=200)
        finally:
            event.remove(connection, "before_cursor_execute", count_statement)
        assert result["records"] == 200
        assert statements[0] <= 25
        db.rollback()


def test_transitive_identity_resolves_across_three_batches():
    prefix = f"resolver_transitive_{uuid.uuid4().hex}"
    with Session(engine) as db:
        db.begin()
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['contact']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        rows = [
            (f"{prefix}_a", f"{prefix}@example.org", None),
            (f"{prefix}_b", f"{prefix}@example.org", "+12147483647"),
            (f"{prefix}_c", None, "+12147483647"),
        ]
        results = []
        for external_id, email, phone in rows:
            db.execute(text("""
                INSERT INTO source_records
                  (source_id, external_id, email_norm, phone_e164, raw_hash)
                VALUES (:source_id, :external_id, :email, :phone, :external_id)
            """), {"source_id": source_id, "external_id": external_id,
                   "email": email, "phone": phone})
            results.append(resolve_batch(db))
        assert [result["records"] for result in results] == [1, 1, 1]
        assert results[0]["profiles_created"] == 1
        assert results[1]["profiles_created"] == results[2]["profiles_created"] == 0
        assert db.execute(text("""
            SELECT count(DISTINCT profile_id) FROM source_records WHERE source_id=:source_id
        """), {"source_id": source_id}).scalar_one() == 1
        assert db.execute(text("""
            SELECT count(*) FROM source_records WHERE source_id=:source_id AND resolved_at IS NOT NULL
        """), {"source_id": source_id}).scalar_one() == 3
        db.rollback()


def test_zeta_enrichment_reference_links_to_crm_and_reimport_is_idempotent():
    prefix = f"resolver_zeta_reference_{uuid.uuid4().hex}"
    crm_external_id = f"crm-{prefix}"
    zeta_external_id = f"zeta-{prefix}"
    with Session(engine) as db:
        db.begin()
        crm_source_id = db.execute(
            text("SELECT id FROM sources WHERE key='donor_crm'")
        ).scalar()
        if crm_source_id is None:
            crm_source_id = db.execute(text("""
                INSERT INTO sources (key, name, kind, record_types, priority, is_active)
                VALUES ('donor_crm', :name, 'csv', ARRAY['contact']::text[], 10, true)
                RETURNING id
            """), {"name": f"{prefix}_CRM"}).scalar_one()
        zeta_source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['enrichment']::text[], 20, true)
            RETURNING id
        """), {"key": prefix}).scalar_one()
        crm_import_id = db.execute(text("""
            INSERT INTO imports (source_id, filename, record_type)
            VALUES (:source_id, 'crm.csv', 'contact') RETURNING id
        """), {"source_id": crm_source_id}).scalar_one()
        db.execute(text("""
            INSERT INTO source_records
              (source_id, external_id, raw_hash, last_import_id)
            VALUES (:source_id, :external_id, 'crm-row', :import_id)
        """), {
            "source_id": crm_source_id, "external_id": crm_external_id,
            "import_id": crm_import_id,
        })
        crm_result = resolve_batch(db)
        assert crm_result["profiles_created"] == 1
        crm_profile_id = db.execute(text("""
            SELECT profile_id FROM source_records
            WHERE source_id=:source_id AND external_id=:external_id
        """), {"source_id": crm_source_id, "external_id": crm_external_id}).scalar_one()

        headers = ["external_id", "contact_external_id", "hh_income_band"]
        mapping = _mapping(headers, "enrichment")
        raw = {
            "external_id": zeta_external_id,
            "contact_external_id": crm_external_id,
            "hh_income_band": "75k_149k",
        }
        values = map_and_validate_row(
            raw, mapping["columns"], "enrichment", mapping["options"]
        )["values"]
        assert values["enrichments"]["hh_income_band"] == ("enum", "75k_149k")
        payload = _row_payload(
            "enrichment", values, mapping["options"]["reference_source"]
        )
        first_import_id = db.execute(text("""
            INSERT INTO imports (source_id, filename, record_type)
            VALUES (:source_id, 'zeta-1.csv', 'enrichment') RETURNING id
        """), {"source_id": zeta_source_id}).scalar_one()
        _upsert_source_records(db, zeta_source_id, first_import_id, [payload])

        resolved = resolve_batch(db)
        assert resolved == {"records": 1, "profiles_created": 0, "merges": 0}
        assert db.execute(text("""
            SELECT profile_id FROM source_records
            WHERE source_id=:source_id AND external_id=:external_id
        """), {"source_id": zeta_source_id, "external_id": zeta_external_id}).scalar_one() == crm_profile_id
        assert db.execute(text("""
            SELECT count(*) FROM identifiers
            WHERE type='external' AND value=ANY(:values) AND profile_id=:profile_id
        """), {
            "values": [f"donor_crm:{crm_external_id}", f"{prefix}:{zeta_external_id}"],
            "profile_id": crm_profile_id,
        }).scalar_one() == 2
        assert db.execute(text("""
            SELECT value_text FROM enrichment_values
            WHERE profile_id=:profile_id AND source_id=:source_id
              AND attribute_key='hh_income_band'
        """), {"profile_id": crm_profile_id, "source_id": zeta_source_id}).scalar_one() == "75k_149k"

        second_import_id = db.execute(text("""
            INSERT INTO imports (source_id, filename, record_type)
            VALUES (:source_id, 'zeta-2.csv', 'enrichment') RETURNING id
        """), {"source_id": zeta_source_id}).scalar_one()
        _upsert_source_records(db, zeta_source_id, second_import_id, [payload])
        assert db.execute(text("""
            SELECT count(*) FROM source_records
            WHERE source_id=:source_id AND external_id=:external_id
        """), {"source_id": zeta_source_id, "external_id": zeta_external_id}).scalar_one() == 1
        assert resolve_batch(db) == {"records": 0, "profiles_created": 0, "merges": 0}
        assert db.execute(text("""
            SELECT count(*) FROM identifiers
            WHERE type='external' AND value=ANY(:values) AND profile_id=:profile_id
        """), {
            "values": [f"donor_crm:{crm_external_id}", f"{prefix}:{zeta_external_id}"],
            "profile_id": crm_profile_id,
        }).scalar_one() == 2
        db.rollback()


def test_component_merging_three_profiles_records_two_merges():
    prefix = f"resolver_multi_merge_{uuid.uuid4().hex}"
    with Session(engine) as db:
        db.begin()
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['contact']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        profiles = [
            db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
            for _ in range(3)
        ]
        email = f"{prefix}@example.org"
        phone = "+12147483647"
        db.execute(text("""
            INSERT INTO identifiers (type, value, profile_id)
            VALUES ('email', :email, :email_profile),
                   ('phone', :phone, :phone_profile),
                   ('external', :external, :external_profile)
        """), {
            "email": email, "email_profile": profiles[0],
            "phone": phone, "phone_profile": profiles[1],
            "external": f"{prefix}:{prefix}_bridge",
            "external_profile": profiles[2],
        })
        db.execute(text("""
            INSERT INTO source_records
              (source_id, external_id, email_norm, phone_e164, raw_hash)
            VALUES (:source_id, :external_id, :email, :phone, 'bridge')
        """), {
            "source_id": source_id, "external_id": f"{prefix}_bridge",
            "email": email, "phone": phone,
        })
        result = resolve_batch(db)
        assert result["merges"] == 2
        assert db.execute(text("""
            SELECT count(*) FROM profile_merges
            WHERE winner_id=:winner AND loser_id=ANY(:losers)
        """), {"winner": profiles[0], "losers": profiles[1:]}).scalar_one() == 2
        assert db.execute(text("""
            SELECT count(DISTINCT profile_id) FROM identifiers
            WHERE value=ANY(:values)
        """), {"values": [email, phone, f"{prefix}:{prefix}_bridge"]}).scalar_one() == 1
        db.rollback()


def test_anonymous_event_backfill_updates_database():
    prefix = f"resolver_anon_{uuid.uuid4().hex}"
    with Session(engine) as db:
        db.begin()
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['event']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        profile_id = db.execute(
            text("INSERT INTO profiles DEFAULT VALUES RETURNING id")
        ).scalar_one()
        occurred_at = datetime.now(timezone.utc)
        db.execute(text("""
            INSERT INTO events (source_id, type, name, occurred_at, anonymous_id)
            VALUES (:source_id, 'track', 'anonymous-page-view', :occurred_at, 'anon-test')
        """), {"source_id": source_id, "occurred_at": occurred_at})
        assert backfill_anonymous_events(db, source_id, "anon-test", profile_id) == 1
        assert backfill_anonymous_events(db, source_id, "anon-test", profile_id) == 0
        assert db.execute(text("""
            SELECT profile_id FROM events WHERE source_id=:source_id AND occurred_at=:occurred_at
        """), {"source_id": source_id, "occurred_at": occurred_at}).scalar_one() == profile_id
        db.rollback()


def test_blocklisted_identifier_never_links_records():
    prefix = f"resolver_blocked_{uuid.uuid4().hex}"
    with Session(engine) as db:
        db.begin()
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['contact']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        db.execute(text("""
            INSERT INTO source_records (source_id, external_id, email_norm, raw_hash)
            SELECT :source_id, :prefix || '-' || n, 'test@test.com', 'row-' || n
            FROM generate_series(1, 500) n
        """), {"source_id": source_id, "prefix": prefix})
        result = resolve_batch(db, limit=500)
        profiles = db.execute(text("""
            SELECT count(DISTINCT profile_id) FROM source_records WHERE source_id=:source_id
        """), {"source_id": source_id}).scalar_one()
        linked_emails = db.execute(text("""
            SELECT count(*) FROM identifiers WHERE type='email' AND value='test@test.com'
        """)).scalar_one()
        assert result["merges"] == 0
        assert profiles == 500
        assert linked_emails == 0
        db.rollback()


def test_high_cardinality_identifier_is_auto_blocklisted():
    prefix = f"resolver_cardinality_{uuid.uuid4().hex}"
    email = f"{prefix}@example.org"
    phone = "+12025550123"
    with Session(engine) as db:
        db.begin()
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['contact']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        import_id = db.execute(text("""
            INSERT INTO imports (source_id, filename, record_type)
            VALUES (:source_id, :filename, 'contact') RETURNING id
        """), {"source_id": source_id, "filename": f"{prefix}.csv"}).scalar_one()
        db.execute(text("""
            INSERT INTO source_records
              (source_id, external_id, email_norm, phone_e164, raw_hash, last_import_id)
            SELECT :source_id, :prefix || '-' || n, :email, :phone, 'row-' || n, :import_id
            FROM generate_series(1, 26) n
        """), {
            "source_id": source_id, "prefix": prefix, "email": email,
            "phone": phone, "import_id": import_id,
        })
        resolve_batch(db, limit=100)
        reasons = db.execute(text("""
            SELECT reason FROM identifier_blocklist WHERE type='email' AND value=:email
        """), {"email": email}).scalar_one()
        phone_reason = db.execute(text("""
            SELECT reason FROM identifier_blocklist WHERE type='phone' AND value=:phone
        """), {"phone": phone}).scalar_one()
        assert reasons == phone_reason == "high_cardinality"
        assert db.execute(text("""
            SELECT count(DISTINCT profile_id) FROM source_records WHERE source_id=:source_id
        """), {"source_id": source_id}).scalar_one() == 26
        db.rollback()


def test_gift_records_do_not_trigger_high_cardinality_for_donor_identifiers():
    prefix = f"resolver_gift_cardinality_{uuid.uuid4().hex}"
    email = f"{prefix}@example.org"
    phone = "+12025550123"
    with Session(engine) as db:
        db.begin()
        source_id = db.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, priority, is_active)
            VALUES (:key, :key, 'csv', ARRAY['gift']::text[], 10, true) RETURNING id
        """), {"key": prefix}).scalar_one()
        import_id = db.execute(text("""
            INSERT INTO imports (source_id, filename, record_type)
            VALUES (:source_id, :filename, 'gift') RETURNING id
        """), {"source_id": source_id, "filename": f"{prefix}.csv"}).scalar_one()
        db.execute(text("""
            INSERT INTO source_records
              (source_id, external_id, email_norm, phone_e164, raw_hash, last_import_id)
            SELECT :source_id, :prefix || '-' || n, :email, :phone,
                   'row-' || n, :import_id
            FROM generate_series(1, 26) n
        """), {
            "source_id": source_id, "prefix": prefix, "email": email,
            "phone": phone, "import_id": import_id,
        })
        db.execute(text("""
            INSERT INTO gifts (source_id, external_id, amount)
            SELECT :source_id, :prefix || '-' || n, 10
            FROM generate_series(1, 26) n
        """), {"source_id": source_id, "prefix": prefix})

        result = resolve_batch(db, limit=100)
        assert result == {"records": 26, "profiles_created": 1, "merges": 0}
        assert db.execute(text("""
            SELECT count(DISTINCT profile_id) FROM source_records WHERE source_id=:source_id
        """), {"source_id": source_id}).scalar_one() == 1
        assert db.execute(text("""
            SELECT count(*) FROM identifier_blocklist
            WHERE (type='email' AND value=:email) OR (type='phone' AND value=:phone)
        """), {"email": email, "phone": phone}).scalar_one() == 0
        assert db.execute(text("""
            SELECT count(DISTINCT profile_id) FROM gifts WHERE source_id=:source_id
        """), {"source_id": source_id}).scalar_one() == 1
        assert db.execute(text("""
            SELECT count(*) FROM gifts WHERE source_id=:source_id AND profile_id IS NOT NULL
        """), {"source_id": source_id}).scalar_one() == 26
        db.rollback()