"""Actual read-only PostgreSQL accuracy loader with compact truth; opt-in schema fixture."""
import sqlite3
from datetime import date

from sqlalchemy import text

from app.seed import generate_seed
from test_consent_ingestion_pg import db
from test_identity_acceptance import identity_acceptance as accuracy


def test_compact_truth_against_actual_postgres_records(db, tmp_path):
    generated = generate_seed(10, output_dir=tmp_path, as_of=date(2026, 10, 17))
    local = sqlite3.connect(str(tmp_path/"accuracy.sqlite"))
    accuracy._sqlite_database(local)
    try:
        accuracy._iter_ground_truth(generated["ground_truth"], local)
        people = [row[0] for row in local.execute("SELECT DISTINCT person_id FROM truth ORDER BY person_id")]
        profiles = {person: db.scalar(text("INSERT INTO profiles DEFAULT VALUES RETURNING id"))
                    for person in people}
        sources = {source: db.scalar(text("""
            INSERT INTO sources(key,name,kind,record_types)
            VALUES(:key,:key,'csv',ARRAY['contact']) RETURNING id
        """), {"key":source}) for source in accuracy.FILES.values()}
        for source, external, person in local.execute("SELECT source_key,external_id,person_id FROM truth"):
            db.execute(text("""
                INSERT INTO source_records(source_id,external_id,profile_id,raw_hash)
                VALUES(:source,:external,:profile,'compact-truth-test')
            """), {"source":sources[source], "external":external, "profile":profiles[person]})
        db.commit()
        def metrics():
            local.execute("DELETE FROM actual")
            accuracy._load_active_records(db.get_bind(), local)
            return accuracy._pairwise_accuracy(local)
        assert accuracy._compact_accuracy_pass(metrics())
        # One person's ESP record on another profile is a real cross-person
        # merge AND a split, even though the total profile count stays correct.
        db.execute(text("""
            UPDATE source_records SET profile_id=:wrong
            WHERE source_id=:source AND external_id='esp-0'
        """), {"wrong":profiles[people[1]], "source":sources["esp"]})
        db.commit()
        wrong = metrics()
        assert wrong["false_positive_pairs_strict"] > 0
        assert wrong["false_negative_pairs_including_unmatched"] > 0
        assert not accuracy._compact_accuracy_pass(wrong)
        # A pure split onto a fresh active profile is also rejected.
        extra = db.scalar(text("INSERT INTO profiles DEFAULT VALUES RETURNING id"))
        db.execute(text("""
            UPDATE source_records SET profile_id=:split
            WHERE source_id=:source AND external_id='esp-0'
        """), {"split":extra, "source":sources["esp"]})
        db.commit()
        split = metrics()
        assert split["false_positive_pairs_strict"] == 0
        assert split["false_negative_pairs_including_unmatched"] > 0
        assert not accuracy._compact_accuracy_pass(split)
    finally:
        local.close()
