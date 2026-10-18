"""M4 integration acceptance; only runs in the disposable benchmark test DB."""
from datetime import date, timedelta

from sqlalchemy import text

from test_consent_ingestion_pg import db
from test_schema_benchmark_pg import admin
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


def test_handcomputed_refund_reference_rfm_snapshots_and_canonical_video(db):
    """Net money, gross donation frequency/recency; refund-only is a prospect."""
    import json
    from decimal import Decimal
    from app.traits.verification import reference
    db.execute(text("INSERT INTO sources(id,key,name,kind) OVERRIDING SYSTEM VALUE VALUES(1,'refund_test','Refund test','csv')"))
    db.execute(text("INSERT INTO profiles(id) OVERRIDING SYSTEM VALUE SELECT generate_series(1,5)"))
    ledger = [
        (1, "old", 100, "2023-01-01", False, False),
        (1, "recent", 50, "2024-12-01", False, False),
        (1, "return", -25, "2024-12-31", True, True),
        (1, "future", 1000, "2025-01-01", False, True),
        (2, "peer", 130, "2024-12-15", False, False),
        (3, "refund_only", -10, "2024-11-29", True, True),
        (5, "lapsed", 30, "2022-01-01", False, False),
        (5, "lapsed_return", -5, "2024-11-30", True, True),
    ]
    for profile, external_id, amount, day, refund, recurring in ledger:
        db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,is_recurring,attributes)
            VALUES(1,:external_id,:profile,:amount,:day,:recurring,CAST(:attributes AS jsonb))"""),
                   {"external_id": external_id, "profile": profile, "amount": amount, "day": date.fromisoformat(day),
                    "recurring": recurring, "attributes": json.dumps({"is_refund": refund})})
    for name, day in (("vod_viewed", "2024-12-30"), ("Video Watched", "2024-12-29"), ("vod_viewed", "2024-11-01")):
        db.execute(text("""INSERT INTO events(source_id,profile_id,type,name,occurred_at)
            VALUES(1,1,'track',:name,CAST(:day AS timestamptz))"""), {"name": name, "day": day})
    pinned = date(2024, 12, 31)
    raw = reference(db, [1, 2, 3, 4, 5], pinned)
    golden = {
        1: {"gift_count_total": 2, "ltv_total": Decimal(125), "gift_amount_12m": Decimal(25),
            "gift_count_12m": 1, "first_gift_date": date(2023, 1, 1), "last_gift_date": date(2024, 12, 1),
            "largest_gift_amount": Decimal(100), "avg_gift_amount": Decimal(75),
            "is_recurring_active": False, "days_since_last_gift": 30, "donor_status": "active",
            "rfm_score": "454", "video_views_30d": 2},
        2: {"gift_count_total": 1, "ltv_total": Decimal(130), "rfm_score": "545"},
        3: {"gift_count_total": 0, "ltv_total": Decimal(-10), "gift_amount_12m": Decimal(-10),
            "gift_count_12m": 0, "last_gift_date": None, "avg_gift_amount": None,
            "is_recurring_active": False, "donor_status": "prospect", "rfm_score": None},
        4: {"gift_count_total": 0, "ltv_total": Decimal(0), "rfm_score": None},
        5: {"gift_count_total": 1, "ltv_total": Decimal(25), "gift_amount_12m": Decimal(-5),
            "last_gift_date": date(2022, 1, 1), "donor_status": "lapsed", "rfm_score": "333"},
    }
    for profile, fields in golden.items():
        for key, value in fields.items():
            assert raw[profile][key] == value, (profile, key)
    recompute_traits(db, as_of=pinned)
    for row in db.execute(text("SELECT * FROM profile_traits ORDER BY profile_id")).mappings():
        for key, value in raw[row["profile_id"]].items():
            assert row[key] == value, (row["profile_id"], key)
    # Recomputing only a refund-only profile and one donor must not rank within
    # this subset or admit refund-only profiles into the global donor population.
    recompute_traits(db, as_of=pinned, profile_ids=[1, 3], write_snapshot=False)
    assert db.scalar(text("SELECT rfm_score FROM profile_traits WHERE profile_id=1")) == "454"
    assert db.scalar(text("SELECT rfm_score FROM profile_traits WHERE profile_id=3")) is None
    backfill_trait_snapshots(db, as_of=pinned)
    assert db.execute(text("""SELECT profile_count,ltv_sum,giving_12m_sum FROM trait_snapshots
        WHERE month='2024-11-01' AND donor_status='lapsed'""")).one() == (1, Decimal(25), Decimal(-5))
    assert db.execute(text("""SELECT profile_count,ltv_sum FROM trait_snapshots
        WHERE month='2024-11-01' AND donor_status='prospect'""")).one() == (3, Decimal(-10))


def test_production_traits_cli_smoke_50_audit_profiles(admin, tmp_path):
    """Exercise the real audit-seeded CLI/oracle, independent of segment smoke."""
    import json
    import os
    import subprocess
    import sys
    engine, url = admin
    def public_state():
        with engine.connect() as connection:
            counts = {table: connection.scalar(text(f"SELECT count(*) FROM public.{table}"))
                      for table in ("profiles", "source_records", "gifts", "events", "jobs")}
            roles = set(connection.scalars(text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'kinship_bench_%'")))
            schemas = set(connection.scalars(text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'kinship_bench_%'")))
        return counts, roles, schemas
    before = public_state()
    env = dict(os.environ, DATABASE_URL=url, APP_ENV="production", AUTH_MODE="oidc",
               OIDC_ISSUER="https://oidc.invalid", OIDC_CLIENT_ID="fixture",
               OIDC_CLIENT_SECRET="fixture", PUBLIC_BASE_URL="https://fixture.invalid",
               SECRET_KEY="x" * 40, KINSHIP_BENCHMARK_BACKEND="schema",
               KINSHIP_BENCHMARK_OUTPUT=str(tmp_path))
    result = subprocess.run([sys.executable, "-m", "app.cli", "benchmark",
                             "--traits", "--profiles", "50", "--as-of", "2026-01-15"],
                            env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=120)
    (tmp_path / "production-traits-cli.txt").write_text(result.stdout)
    assert result.returncode == 0, result.stdout[-10000:]
    report = json.loads(next(tmp_path.glob("traits-*/traits.json")).read_text())
    assert report["status"] == "passed" and report["requested_people"] == 50
    assert report["verification_scope"] == "small-population-smoke"
    assert report["as_of"] == "2026-01-15" and report["discrepancies"] == []
    assert report["unresolved_records"] == 0 and report["compared_profiles"] > 0
    assert public_state() == before


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
    assert calls == [{"job_id": None}]
    assert db.execute(text("SELECT count(*) FROM trait_dirty_profiles")).scalar_one() == 0
    assert db.execute(text("SELECT count(*) FROM profile_traits")).scalar_one() == 10
    db.execute(text("INSERT INTO trait_dirty_profiles(profile_id,dirtied_at) VALUES(2,clock_timestamp()-interval '11 minutes'),(3,clock_timestamp())"))
    handlers.run("traits.recompute", {"mode": "dirty"})
    assert calls[-1] == {"profile_ids": [2], "job_id": None}
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


def test_reference_date_advances_status_without_using_dataset_max_and_republishes(db):
    from app.segments.verification import acceptance_summary
    pinned, _ = fixture(db)
    recompute_traits(db, as_of=pinned)
    before = acceptance_summary(db, pinned)
    assert before["current_status_publication_matches"]
    assert before["giver_groups"]["active_ish"]["count"] > 0
    assert before["dashboard_last_12m"]["nonempty"]
    generation = db.scalar(text("SELECT value FROM dashboard_kpis WHERE key='generation'"))

    later = pinned + timedelta(days=731)
    recompute_traits(db, as_of=later)
    after = acceptance_summary(db, later)
    assert after["current_status_publication_matches"]
    assert after["giver_groups"]["active_ish"]["count"] == 0
    assert after["giver_groups"]["lapsing"]["count"] == 0
    assert after["giver_groups"]["lapsed"]["count"] == after["givers"] == 9
    assert not after["dashboard_last_12m"]["nonempty"]
    assert db.scalar(text("SELECT value FROM dashboard_kpis WHERE key='generation'")) != generation
    # No new data was inserted; age must nevertheless advance by the explicit clock.
    assert db.scalar(text("SELECT days_since_last_gift FROM profile_traits WHERE profile_id=9")) == 731


def test_current_month_backfill_and_rfm_ignore_future_gifts(db):
    pinned, _ = fixture(db)
    db.execute(text("""
        INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date)
        VALUES(1,'future-donor',1,100000,:future)
    """), {"future": pinned + timedelta(days=1)})
    db.execute(text("DELETE FROM trait_snapshots"))
    midmonth = pinned - timedelta(days=14)
    backfill_trait_snapshots(db, as_of=midmonth)
    assert db.scalar(text("""
        SELECT sum(ltv_sum) FROM trait_snapshots WHERE month=:month
    """), {"month": midmonth.replace(day=1)}) == db.scalar(text("""
        SELECT sum(amount) FROM gifts WHERE gift_date<=:as_of
    """), {"as_of": midmonth})
    recompute_traits(db, as_of=pinned)
    assert db.scalar(text("SELECT rfm_score FROM profile_traits WHERE profile_id=1")) is None
    recompute_traits(db, as_of=pinned + timedelta(days=1))
    assert db.scalar(text("SELECT rfm_monetary FROM profile_traits WHERE profile_id=1")) == 5


def test_trait_event_windows_are_utc_even_with_non_utc_database_timezone(db):
    from datetime import datetime, timezone
    pinned, _ = fixture(db)
    db.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))
    db.execute(text("""
        INSERT INTO events(source_id,message_id,profile_id,type,name,occurred_at)
        VALUES(1,'utc-start',1,'track','Video Watched',:start),
              (1,'utc-end',1,'track','Video Watched',:end),
              (1,'utc-next',1,'track','Video Watched',:future)
    """), {"start": datetime(2024, 12, 2, tzinfo=timezone.utc),
          "end": datetime(2024, 12, 31, 23, 59, tzinfo=timezone.utc),
          "future": datetime(2025, 1, 1, tzinfo=timezone.utc)})
    recompute_traits(db, as_of=pinned)
    assert db.scalar(text("SELECT video_views_30d FROM profile_traits WHERE profile_id=1")) == 2
    assert db.scalar(text("SELECT events FROM dashboard_event_daily WHERE day='2024-12-31'")) == 1


def test_segment_reference_counts_do_not_depend_on_persisted_traits(db):
    from app.segments.verification import REFERENCE_SQL
    pinned, _ = fixture(db)
    recompute_traits(db, as_of=pinned)
    expected = {key: db.scalar(text(sql), {"as_of": pinned}) for key, sql in REFERENCE_SQL.items()}
    assert expected["new-partners"] > 0 and expected["top-giving"] > 0
    db.execute(text("DELETE FROM profile_traits"))
    assert {key: db.scalar(text(sql), {"as_of": pinned}) for key, sql in REFERENCE_SQL.items()} == expected