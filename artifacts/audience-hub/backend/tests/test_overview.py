"""Small transactional integration fixtures; never run against production."""
import os
import subprocess
import sys
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.dashboards import api
from app.dashboards.overview import delta, month_shift


@pytest.fixture
def db():
    if (os.getenv("DATABASE_URL") != "postgresql+psycopg:///ah_overview_tests"
            and os.getenv("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1"):
        pytest.skip("Requires dedicated isolated ah_overview_tests schema database")
    from app.db import engine
    with Session(engine) as session:
        try:
            yield session
        finally:
            session.rollback()


def test_delta_and_leap_dates():
    assert delta(0, 0)["delta_label"] == "—"
    assert delta(2, 0)["delta_label"] == "New"
    assert delta(2, 1)["change_pct"] == 100
    assert month_shift(date(2024, 2, 29), -12) == date(2023, 2, 28)


def test_email_cursor_cleanup_preserves_deadline(monkeypatch):
    from fastapi import HTTPException
    from app.dashboards import cache
    calls, closed = [], []

    def check_deadline(db=None):
        calls.append(db)
        if len(calls) == 2:
            raise HTTPException(504, "original deadline")

    def close():
        closed.append(True)
        raise RuntimeError("cursor close failed after cancellation")

    result = SimpleNamespace(mappings=lambda: None, close=close)
    db = SimpleNamespace(execute=lambda *args, **kwargs: result)
    monkeypatch.setattr(api, "_rows", lambda *args: [{"value_hash": "suppressed"}])
    monkeypatch.setattr(cache, "check_deadline", check_deadline)
    with pytest.raises(HTTPException) as error:
        api._email_opted_in_count(db)
    assert error.value.status_code == 504
    assert error.value.detail == "original deadline"
    assert calls == [db, None]  # no SQL deadline reset while cursor is open
    assert closed == [True]


def test_default_range_with_no_historical_data(db):
    result = api._dashboard("overview", None, None, db)
    assert result["range"]["to"] == date.today().isoformat()
    assert result["range"]["from"] == date.today().replace(day=1).isoformat()
    assert result["overview"]["kpis"]["giving"]["value"] == 0
    assert result["overview"]["kpis"]["retention_yoy"]["denominator"] == 0
    assert len(result["overview"]["kpis"]["giving"]["sparkline"]) == 12


def test_real_statement_deadline_is_explicit(db, monkeypatch):
    from fastapi import HTTPException
    from app.dashboards import cache
    monkeypatch.setattr(cache, "DEADLINE_SECONDS", .1)
    with pytest.raises(HTTPException) as error:
        with cache.deadline(db):
            api._scalar(db, "SELECT pg_sleep(1)")
    assert error.value.status_code == 504


def test_consent_source_duplication_optout_and_unicode_suppression(db):
    import hashlib
    import hmac
    from app.config import get_settings
    source = db.execute(text("""INSERT INTO sources(key,name,kind)
        VALUES ('consent-test','Test','crm') RETURNING id""")).scalar_one()
    ids = [db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
           for _ in range(5)]
    for i, pid in enumerate(ids):
        email = "Straße@example.org" if i == 0 else f"consent{i}@example.org"
        db.execute(text("""INSERT INTO source_records
            (source_id,external_id,profile_id,email_norm,attributes,raw_hash)
            SELECT :s, :prefix||n, :p, :email, '{"email_consent":"opted_in"}', md5(n::text)
            FROM generate_series(1,20) n"""),
            {"s": source, "prefix": str(i)+"-", "p": pid, "email": email})
    db.execute(text("""INSERT INTO consents(profile_id,channel,status,source_id)
        VALUES (:p,'email','opted_out',:s)"""), {"p": ids[1], "s": source})
    db.execute(text("UPDATE profiles SET is_deleted=true WHERE id=:p"), {"p": ids[2]})
    db.execute(text("UPDATE profiles SET merged_into_id=:winner WHERE id=:p"),
               {"p": ids[3], "winner": ids[4]})
    assert api._email_opted_in_count(db) == 2
    digest = hmac.new(get_settings().pii_hash_pepper.encode(),
                      "Straße@example.org".casefold().encode(), hashlib.sha256).hexdigest()
    db.execute(text("""INSERT INTO suppressions(type,value_hash,reason)
        VALUES ('email',:h,'manual_test')"""), {"h": digest})
    assert api._email_opted_in_count(db) == 1
    # An unrelated suppressed email must not block this profile's second email.
    db.execute(text("""INSERT INTO source_records
        (source_id,external_id,profile_id,email_norm,attributes,raw_hash)
        VALUES (:s,'alternate',:p,'alternate@example.org','{"email_consent":"opted_in"}','a')"""),
        {"s": source, "p": ids[0]})
    assert api._email_opted_in_count(db) == 2


def test_other_api_process_observes_committed_changes_without_ttl(db):
    script = """
import sys
from datetime import date
from sqlalchemy.orm import Session
from app.db import engine
from app.dashboards.api import _dashboard
for line in sys.stdin:
    with Session(engine) as db:
        print(_dashboard('overview', date(2025,1,1), date(2025,1,31), db)
              ['overview']['stats']['profiles']['value'], flush=True)
"""
    child = subprocess.Popen([sys.executable, "-u", "-c", script],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    pid = None
    try:
        child.stdin.write("read\n")
        child.stdin.flush()
        before = int(child.stdout.readline())
        pid = db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
        db.commit()
        child.stdin.write("read\n")
        child.stdin.flush()
        assert int(child.stdout.readline()) == before + 1
    finally:
        child.stdin.close()
        child.wait(timeout=10)
        if pid is not None:
            db.execute(text("DELETE FROM profiles WHERE id=:id"), {"id": pid})
            db.commit()


@pytest.mark.parametrize("pepper", ["short-key", "long-key-" * 20])
def test_email_sql_hmac_matches_python_and_unicode_fallback(db, monkeypatch, pepper):
    import hashlib
    import hmac
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(pii_hash_pepper=pepper))
    source = db.execute(text("""INSERT INTO sources(key,name,kind)
        VALUES ('hmac-test','Test','crm') RETURNING id""")).scalar_one()
    emails = [
        "ASCII@EXAMPLE.ORG", "\ttrim@example.org\r\n", "\x1cstrip@example.org\x1f",
        "Straße@example.org", "İ@example.org", "Σς@example.org",
        "\u2003wide@example.org\u00a0", "K@example.org",
    ]
    for index, email in enumerate(emails):
        pid = db.execute(text("INSERT INTO profiles(email) VALUES (:e) RETURNING id"),
                         {"e": email}).scalar_one()
        db.execute(text("""INSERT INTO consents(profile_id,channel,status,source_id)
            VALUES (:p,'email','granted',:s)"""), {"p": pid, "s": source})
        # Preserve the existing candidate SQL normalization, then Python strip/casefold.
        normalized = db.execute(text("SELECT lower(btrim(CAST(:e AS text)))"),
                                {"e": email}).scalar_one()
        digest = hmac.new(pepper.encode(), normalized.strip().casefold().encode(),
                          hashlib.sha256).hexdigest()
        db.execute(text("""INSERT INTO suppressions(type,value_hash,reason)
            VALUES ('email',:h,'manual_test')"""), {"h": digest})
    assert api._email_opted_in_count(db) == 0
    # A non-suppressed ASCII address accepts even a Unicode-suppressed profile.
    db.execute(text("""INSERT INTO identifiers(type,value,profile_id)
        VALUES ('email','allowed@example.org',:p)"""), {"p": pid})
    assert api._email_opted_in_count(db) == 1
    db.execute(text("DELETE FROM suppressions WHERE reason='manual_test'"))
    assert api._email_opted_in_count(db) == len(emails)


@pytest.mark.skipif(os.getenv("OVERVIEW_EMAIL_SCALE_TEST") != "1",
                    reason="Explicit opt-in temporary-table 500k profile benchmark")
def test_email_count_scale(db):
    """No persistent fixture writes: temporary tables shadow the dedicated test DB."""
    import ast
    import hashlib
    import hmac
    import inspect
    import time
    from app.config import get_settings
    for ddl in (
        """CREATE TEMP TABLE active_profiles AS
           SELECT n::bigint AS id, 'scale'||n||'@example.org' AS email
           FROM generate_series(1,500000) n""",
        """CREATE TEMP TABLE source_records AS
           SELECT (n%500000+1)::bigint AS profile_id,
             'scale'||(n%500000+1)||'@example.org' AS email_norm,
             '{"email_consent":"opted_in"}'::jsonb AS attributes
           FROM generate_series(1,2900000) n""",
        """CREATE TEMP TABLE consents AS
           SELECT id AS profile_id, 'email'::text AS channel, 'opted_in'::text AS status
           FROM active_profiles""",
        "CREATE TEMP TABLE identifiers(profile_id bigint, type text, value text)",
        "CREATE TEMP TABLE suppressions(type text, value_hash text, reason text)",
    ):
        db.execute(text(ddl))
    pepper = get_settings().pii_hash_pepper.encode()
    digest = hmac.new(pepper, b"scale1@example.org", hashlib.sha256).hexdigest()
    db.execute(text("INSERT INTO suppressions VALUES ('email',:h,'hard_bounce')"), {"h": digest})
    for table in ("active_profiles", "source_records", "consents", "identifiers", "suppressions"):
        db.execute(text(f"ANALYZE {table}"))
    # The previous production algorithm: source address aggregation, global
    # DISTINCT/sort, then transfer and Python HMAC of every accepted profile.
    tree = ast.parse(inspect.getsource(api._email_opted_in_count))
    candidates = next(node.value.value for node in ast.walk(tree)
                      if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "candidates" for t in node.targets))
    candidates = (candidates.split("        ), primary_emails AS (")[0]
                  + "        ), source_signals AS ("
                  + candidates.split("        ), source_signals AS (")[1])
    candidates = candidates.replace("eligible_all AS", "eligible AS")
    candidates = candidates.replace(
        "lower(attributes->>'email_consent')='opted_in' AS opted_in",
        "bool_or(lower(attributes->>'email_consent')='opted_in') AS opted_in"
    ).replace(
        "GROUP BY profile_id, lower(btrim(email_norm::text))", ""
    ).replace(
        "WHERE profile_id IS NOT NULL AND btrim(email_norm::text)<>''",
        """WHERE profile_id IS NOT NULL AND btrim(email_norm::text)<>''
          GROUP BY profile_id, lower(btrim(email_norm::text))"""
    )
    started = time.perf_counter()
    result = db.execute(text(candidates + """
        SELECT DISTINCT profile_id,email FROM candidates ORDER BY profile_id,email
    """), execution_options={"stream_results": True, "yield_per": 2048})
    count, accepted, transferred = 0, None, 0
    try:
        for row in result.mappings():
            transferred += 1
            if row["profile_id"] != accepted and hmac.new(
                    pepper, row["email"].strip().casefold().encode(),
                    hashlib.sha256).hexdigest() != digest:
                count += 1
                accepted = row["profile_id"]
    finally:
        result.close()
    old_seconds = time.perf_counter() - started
    started = time.perf_counter()
    assert api._email_opted_in_count(db) == count == 499999
    new_seconds = time.perf_counter() - started
    print(f"\n500k profiles / 2.9m source rows: legacy={old_seconds:.3f}s "
          f"SQL HMAC={new_seconds:.3f}s; legacy transferred={transferred}; "
          "optimized transferred=1 aggregate row (all ASCII)")


def test_resolved_population_giving_and_period_contract(db):
    source = db.execute(text("INSERT INTO sources(key,name,kind) VALUES ('overview-test','Test','crm') RETURNING id")).scalar_one()
    ids = [db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one() for _ in range(4)]
    db.execute(text("UPDATE profiles SET merged_into_id=:winner WHERE id=:loser"), {"winner": ids[0], "loser": ids[1]})
    db.execute(text("UPDATE profiles SET is_deleted=true WHERE id=:id"), {"id": ids[2]})
    for i, pid in enumerate([ids[0], ids[0], ids[1], ids[2], None]):
        db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,campaign)
             VALUES (:source,:external,:pid,:amount,'2025-03-15','Spring')"""),
                   {"source": source, "external": str(i), "pid": pid, "amount": 10 if pid == ids[0] else 10000})
    result = api._dashboard("overview", date(2025, 3, 10), date(2025, 3, 20), db)
    o = result["overview"]
    assert o["kpis"]["giving"]["value"] == 20
    assert o["kpis"]["active_partners"]["value"] == 1
    assert o["kpis"]["average_gift"]["value"] == 10
    assert o["kpis"]["giving"]["delta_label"] == "New"
    assert o["stats"]["profiles"]["value"] == 2
    assert o["stats"]["email_opted_in"]["value"] <= 2
    assert result["metrics"]["donors"]["value"] <= 2
    assert o["partner_status"]["givers"] == 1
    assert sum(x["count"] for x in o["partner_status"]["statuses"]) == 1
    assert o["partner_status"]["prospects"] == 1
    assert o["campaigns"][0]["share"] == 100
    assert o["campaigns"][0]["gifts"] == 2
    for kpi in o["kpis"].values():
        assert len(kpi["sparkline"]) == 12
        assert kpi["sparkline"][-1]["month"] == "2025-03-01"
    assert o["kpis"]["giving"]["sparkline"][-1]["value"] == 20
    assert o["monthly_giving"][0]["prior_from"] == "2025-02-27"
    assert o["monthly_giving"][0]["prior_to"] == "2025-03-09"
    # Stale process-local entries must never mask newly committed/live data.
    api._cache[("overview", date(2025, 3, 10), date(2025, 3, 20))] = (float("inf"), {"stale": True})
    assert "overview" in api._dashboard("overview", date(2025, 3, 10), date(2025, 3, 20), db)


def test_retention_exclusive_statuses_and_role_safe_shell(db):
    source = db.execute(text("INSERT INTO sources(key,name,kind) VALUES ('status-test','Test','crm') RETURNING id")).scalar_one()
    histories = [
        ["2023-01-01", "2024-06-01", "2025-06-01"],  # active and retained
        ["2025-06-02"],  # new
        ["2020-01-01", "2025-06-03"],  # reactivated
        ["2023-08-01"],  # lapsing
        ["2020-02-01"],  # lapsed
        [],  # prospect
    ]
    for index, history in enumerate(histories):
        pid = db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
        for n, day in enumerate(history):
            db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date)
                VALUES (:s,:x,:p,10,:day)"""), {"s": source, "x": f"{index}-{n}", "p": pid, "day": date.fromisoformat(day)})
    result = api._dashboard("overview", date(2025, 6, 1), date(2025, 6, 30), db)["overview"]
    assert result["kpis"]["retention_yoy"]["value"] == 100
    assert result["kpis"]["retention_yoy"]["denominator"] == 1
    assert result["kpis"]["active_partners"]["value"] == 3
    assert [r["count"] for r in result["partner_status"]["statuses"]] == [1, 1, 1, 1, 1]
    assert result["partner_status"]["prospects"] == 1
    imp = db.execute(text("""INSERT INTO imports(source_id,filename,record_type,status,
        rows_total,rows_ok,rows_rejected,started_at)
        VALUES (:s,'sensitive.csv','gifts','running',100,35,5,now()-interval '10 seconds')
        RETURNING id"""), {"s": source}).scalar_one()
    db.execute(text("""INSERT INTO jobs(type,status,payload)
        VALUES ('import.run','running',jsonb_build_object('import_id',CAST(:id AS bigint)))"""), {"id": imp})
    viewer = api.shell_summary(SimpleNamespace(role="viewer"), db)
    assert viewer["active_import"] is None
    assert "sensitive" not in str(viewer)
    analyst = api.shell_summary(SimpleNamespace(role="analyst"), db)
    assert analyst["imports_running"] == 1
    assert analyst["active_import"]["done"] == 40
    assert analyst["active_import"]["percent"] == 40
    assert analyst["active_import"]["rows_per_second"] == 4


@pytest.mark.parametrize("start,end", [
    (date(2025, 1, 1), date(2025, 1, 30)),
    (date(2025, 1, 1), date(2025, 3, 31)),
    (date(2024, 3, 1), date(2025, 2, 28)),
    (date(2025, 1, 1), date(2025, 6, 17)),
    (date(2023, 7, 1), date(2024, 6, 30)),
    (date(2024, 2, 29), date(2024, 3, 2)),
])
def test_empty_ranges_are_zero_filled_and_prior_aligned(db, start, end):
    result = api._dashboard("overview", start, end, db)
    o = result["overview"]
    assert o["kpis"]["giving"]["delta_label"] == "—"
    assert o["top_two_month_share"] is None
    assert o["campaigns"] == []
    for metric in o["kpis"].values():
        assert len(metric["sparkline"]) == 12
        assert all(p["value"] == 0 for p in metric["sparkline"])
    buckets = o["monthly_giving"]
    assert buckets[0]["from"] == start.isoformat()
    assert buckets[-1]["to"] == end.isoformat()
    assert buckets[0]["prior_from"] == result["range"]["prior_from"]
    assert buckets[-1]["prior_to"] == result["range"]["prior_to"]
    for previous, current in zip(buckets, buckets[1:]):
        assert (date.fromisoformat(current["prior_from"])-date.fromisoformat(previous["prior_to"])).days == 1
    assert api.shell_summary(SimpleNamespace(role="viewer"), db)["imports_running"] is None