"""M4 integration acceptance; only runs in the disposable benchmark test DB."""
from datetime import date, timedelta

from sqlalchemy import text

from test_consent_ingestion_pg import db
from app.traits.engine import recompute_traits, backfill_trait_snapshots
from app.traits.verification import donor_status, quintiles


def fixture(db):
    db.execute(text("INSERT INTO sources(id,key,name,kind) OVERRIDING SYSTEM VALUE VALUES(1,'m4','M4','csv')"))
    db.execute(text("INSERT INTO profiles(id) OVERRIDING SYSTEM VALUE SELECT generate_series(1,10)"))
    pinned = date(2024, 12, 31)
    ages = [[], [365], [366], [730], [731], [900, 0], [730, 0], [731, 0], [0], [364]]
    for p, days in enumerate(ages, 1):
        for n, age in enumerate(days):
            db.execute(text("INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date) "
                            "VALUES(1,:key,:p,:amount,:day)"),
                       dict(key=f"{p}-{n}", p=p, amount=p * 10, day=pinned - timedelta(days=age)))
    return pinned, ages


def test_sql_boundaries_pinned_date_and_global_incremental_rfm(db):
    pinned, ages = fixture(db)
    recompute_traits(db, as_of=pinned)
    rows = list(db.execute(text("SELECT * FROM profile_traits ORDER BY profile_id")).mappings())
    assert [r["donor_status"] for r in rows] == [
        donor_status([pinned - timedelta(days=d) for d in days], pinned) for days in ages]
    donors = {p: days for p, days in enumerate(ages, 1) if days}
    expected = [
        quintiles({p: min(days) for p, days in donors.items()}),
        quintiles({p: len(days) for p, days in donors.items()}, True),
        quintiles({p: p * 10 * len(days) for p, days in donors.items()}, True),
    ]
    for r in rows:
        assert [r[k] for k in ("rfm_recency", "rfm_frequency", "rfm_monetary")] == [rank.get(r["profile_id"]) for rank in expected]
    recompute_traits(db, as_of=pinned, profile_ids=[2, 6], write_snapshot=False)
    assert db.execute(text("SELECT days_since_last_gift FROM profile_traits WHERE profile_id=2")).scalar_one() == 365
    assert db.execute(text("SELECT rfm_score FROM profile_traits WHERE profile_id=6")).scalar_one() == rows[5]["rfm_score"]


def test_snapshot_suppression_escalation_immutability_backfill(db):
    pinned, _ = fixture(db)
    db.execute(text("INSERT INTO source_records(source_id,external_id,raw_hash) "
                    "VALUES(1,'unresolved','m4')"))
    recompute_traits(db, as_of=pinned)
    assert db.execute(text("SELECT count(*) FROM trait_snapshots")).scalar_one() == 0
    db.execute(text("DELETE FROM source_records"))
    db.execute(text("INSERT INTO jobs(type,payload,status) VALUES('import.run','{}','queued')"))
    recompute_traits(db, as_of=pinned)
    assert db.execute(text("SELECT count(*) FROM trait_snapshots")).scalar_one() == 0
    db.execute(text("DELETE FROM jobs"))
    db.execute(text("DELETE FROM profile_traits"))
    # Missing first snapshot forces a full population pass even for one dirty ID.
    assert recompute_traits(db, as_of=pinned, profile_ids=[1]) == 10
    before = db.execute(text("SELECT * FROM trait_snapshots ORDER BY month,donor_status")).all()
    db.execute(text("UPDATE gifts SET amount=amount+1"))
    recompute_traits(db, as_of=pinned)
    assert db.execute(text("SELECT * FROM trait_snapshots ORDER BY month,donor_status")).all() == before
    assert backfill_trait_snapshots(db, as_of=pinned) == 23
    assert backfill_trait_snapshots(db, as_of=pinned) == 0
    assert db.execute(text("SELECT count(DISTINCT month) FROM trait_snapshots")).scalar_one() == 24


def test_real_full_and_dirty_job_handlers(db, monkeypatch):
    from app.jobs import handlers
    import app.db
    import app.traits.engine as traits
    import sqlalchemy.orm
    from contextlib import contextmanager

    pinned, _ = fixture(db)
    # Keep test schema and transaction; actual handler + engine SQL, pinned clock.
    @contextmanager
    def session(_):
        yield db
    monkeypatch.setattr(sqlalchemy.orm, "Session", session)
    monkeypatch.setattr(db, "commit", lambda: db.flush())
    original = traits.recompute_traits
    original_backfill = traits.backfill_trait_snapshots
    calls = []
    def pinned_recompute(session, **kwargs):
        calls.append(kwargs)
        return original(session, as_of=pinned, **kwargs)
    monkeypatch.setattr(traits, "recompute_traits", pinned_recompute)
    monkeypatch.setattr(traits, "backfill_trait_snapshots", lambda session: original_backfill(session, as_of=pinned))
    db.execute(text("INSERT INTO trait_dirty_profiles(profile_id,dirtied_at) VALUES(2,clock_timestamp()-interval '11 minutes') ON CONFLICT DO NOTHING"))
    handlers.run("traits.recompute", {"mode": "full"})
    assert calls == [{}]
    assert db.execute(text("SELECT count(*) FROM trait_dirty_profiles")).scalar_one() == 0
    assert db.execute(text("SELECT count(*) FROM profile_traits")).scalar_one() == 10
    db.execute(text("INSERT INTO trait_dirty_profiles(profile_id,dirtied_at) VALUES(2,clock_timestamp()-interval '11 minutes'),(3,clock_timestamp())"))
    handlers.run("traits.recompute", {"mode": "dirty"})
    assert calls[-1] == {"profile_ids": [2]}
    assert db.execute(text("SELECT profile_id FROM trait_dirty_profiles")).scalars().all() == [3]


def test_identity_resolution_marks_profiles_dirty(db):
    from app.identity.resolver import resolve_batch
    db.execute(text("INSERT INTO sources(id,key,name,kind) OVERRIDING SYSTEM VALUE VALUES(1,'m4','M4','csv')"))
    db.execute(text("INSERT INTO source_records(source_id,external_id,email_norm,raw_hash) "
                    "VALUES(1,'dirty','m4-dirty@example.org','m4')"))
    assert resolve_batch(db)["records"] == 1
    profile = db.execute(text("SELECT profile_id FROM source_records")).scalar_one()
    assert db.execute(text("SELECT profile_id FROM trait_dirty_profiles")).scalar_one() == profile


def test_all_registry_fields_with_future_null_and_window_edges(db):
    from app.traits.verification import reference
    from app.traits.registry import TRAITS_BY_KEY
    pinned, _ = fixture(db)
    for key, age, amount in (("future", -1, 10000), ("undated", None, 7),
                              ("recent44", 44, 1), ("old45", 45, 2),
                              ("window364", 364, 3), ("outside365", 365, 4)):
        db.execute(text("INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,is_recurring) "
                        "VALUES(1,:key,1,:amount,:day,true)"),
                   dict(key=key, amount=amount, day=pinned-timedelta(days=age) if age is not None else None))
    db.execute(text("INSERT INTO events(source_id,message_id,profile_id,type,name,occurred_at) VALUES "
                    "(1,'video-edge',1,'track','Video Watched','2024-12-02 00:00:00Z'),"
                    "(1,'video-outside',1,'track','Video Watched','2024-12-01 23:59:59Z'),"
                    "(1,'video-end',1,'track','Video Watched','2024-12-31 23:59:59Z'),"
                    "(1,'video-future',1,'track','Video Watched','2025-01-01 00:00:00Z')"))
    start = db.execute(text("SELECT clock_timestamp()")).scalar_one()
    recompute_traits(db, as_of=pinned)
    end = db.execute(text("SELECT clock_timestamp()")).scalar_one()
    expected = reference(db, list(range(1, 11)), pinned)
    assert set(expected[1]) | {"computed_at"} == set(TRAITS_BY_KEY)
    actual = list(db.execute(text("SELECT * FROM profile_traits ORDER BY profile_id")).mappings())
    assert len(actual) == 10
    for row in actual:
        assert start <= row["computed_at"] <= end
        assert {key: row[key] for key in expected[row["profile_id"]]} == expected[row["profile_id"]]
    assert actual[0]["video_views_30d"] == 2
    assert actual[0]["is_recurring_active"] is True