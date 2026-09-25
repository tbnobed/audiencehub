"""Synthetic overview workload, only invoked inside the disposable PG harness."""
import json
import hashlib
import hmac
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import date
from types import SimpleNamespace


def measure(directory, gift_rows, source_rows):
    # Never permit this workload to target an app database, even accidentally.
    url = os.environ.get("DATABASE_URL", "")
    if "benchmark@/benchmark?host=/tmp/kinship-bench-" not in url or os.environ.get("APP_ENV") != "test":
        raise RuntimeError("Overview benchmark requires the disposable local PostgreSQL harness")
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.dashboards.api import _endpoint
    env = dict(os.environ, KINSHIP_BENCHMARK_ISOLATED_TESTS="1")
    test = subprocess.run([sys.executable, "-m", "pytest", "tests/test_overview.py",
                           "tests/test_dashboards.py", "tests/test_dashboard_cache.py", "-q"], env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (directory/"overview-tests.txt").write_text(test.stdout)
    if test.returncode:
        raise RuntimeError(test.stdout)
    population = min(250_000, gift_rows)
    with engine.connect() as db:
        db.execute(text("INSERT INTO sources(key,name,kind) VALUES ('scale','Scale','crm')"))
        db.execute(text("""
            INSERT INTO profiles(email) SELECT 'scale'||i||'@example.org'
            FROM generate_series(1,:n) i
        """), {"n": population})
        # Profile ids need not start at one after the isolated regression tests.
        db.execute(text("""CREATE TEMP TABLE benchmark_profiles AS
            SELECT id, row_number() OVER(ORDER BY id) AS n FROM profiles"""))
        db.commit()
        for low in range(1, gift_rows+1, 100_000):
            db.execute(text("""
            INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,is_recurring,campaign)
            SELECT (SELECT id FROM sources WHERE key='scale'), 'gift-'||i, p.id, 10,
              date '2020-01-01'+(i%2192)::int, i%10=0, 'Campaign '||(i%8)
            FROM generate_series(CAST(:low AS bigint),CAST(:n AS bigint)) i
            JOIN benchmark_profiles p ON p.n=(i%:population)+1
        """), {"low": low, "n": min(low+99_999, gift_rows), "population": population})
            db.commit()
        for low in range(1, source_rows+1, 100_000):
            db.execute(text("""
            INSERT INTO source_records(source_id,external_id,profile_id,email_norm,attributes,resolved_at,raw_hash)
            SELECT (SELECT id FROM sources WHERE key='scale'), 'source-'||i, p.id,
              'scale'||p.n||'@example.org', '{"email_consent":"opted_in"}'::jsonb,
              CASE WHEN i%10=0 THEN NULL ELSE now() END, md5(i::text)
            FROM generate_series(CAST(:low AS bigint),CAST(:n AS bigint)) i
            JOIN benchmark_profiles p ON p.n=(i%:population)+1
        """), {"low": low, "n": min(low+99_999, source_rows), "population": population})
            db.commit()
        db.execute(text("""INSERT INTO consents(profile_id,channel,status,source_id)
            SELECT id,'email','opted_in',(SELECT id FROM sources WHERE key='scale') FROM profiles"""))
        from app.config import get_settings
        digest = hmac.new(get_settings().pii_hash_pepper.encode(),
                          b"scale1@example.org", hashlib.sha256).hexdigest()
        db.execute(text("""INSERT INTO suppressions(type,value_hash,reason)
            VALUES ('email',:digest,'hard_bounce')"""), {"digest": digest})
        db.commit()
    measure_existing(directory, gift_rows, source_rows, population, population-1)


def measure_existing(directory, gift_rows, source_rows, population, expected_opted=None):
    """Measure a partially committed disposable fixture after harness interruption."""
    url = os.environ.get("DATABASE_URL", "")
    if "benchmark@/benchmark?host=/tmp/kinship-bench-" not in url or os.environ.get("APP_ENV") != "test":
        raise RuntimeError("Only the disposable benchmark cluster may be measured")
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.dashboards.api import _endpoint
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
        db.execute(text("ANALYZE"))
    timings, payloads = [], []
    for _ in range(2):
        start = time.perf_counter()
        try:
            with Session(engine) as db:
                payload = _endpoint("overview")(
                    date(2025, 1, 1), date(2025, 12, 31), SimpleNamespace(role="viewer"), db)
        except Exception:
            print(f"Dashboard failed after {time.perf_counter()-start:.3f}s", flush=True)
            raise
        timings.append(time.perf_counter()-start)
        payloads.append(payload)
    with engine.connect() as db:
        counts = dict(db.execute(text("""SELECT
          (SELECT count(*) FROM gifts) AS gifts,
          (SELECT count(*) FROM source_records) AS source_records,
          (SELECT count(*) FROM profiles) AS profiles,
          (SELECT count(*) FROM consents) AS consents""")).mappings().one())
        expected = float(db.execute(text("""SELECT COALESCE(sum(amount),0) FROM gifts
            WHERE gift_date BETWEEN '2025-01-01' AND '2025-12-31'""")).scalar_one())
    assert counts["gifts"] == gift_rows and counts["source_records"] == source_rows
    # Concurrent resolution can legitimately change the live attention counts.
    assert all(payloads[0][k] == payloads[1][k] for k in ("range", "metrics", "charts"))
    assert ({k: v for k, v in payloads[0]["overview"].items() if k != "attention"} ==
            {k: v for k, v in payloads[1]["overview"].items() if k != "attention"})
    assert payload["overview"]["stats"]["email_opted_in"]["value"] == (
        population if expected_opted is None else expected_opted)
    assert payload["overview"]["kpis"]["giving"]["value"] == expected
    report = {"counts": counts, "cold_seconds": timings[0], "warm_seconds": timings[1],
              "giving": expected, "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
              "scope": "Committed synthetic gifts/source records; migrated isolated PostgreSQL; "
                       "no concurrent identity worker; unprofiled application dashboard calls.",
              "checks": "counts, scalar SQL giving, email population, identical aggregate payload; "
                        "live attention may differ under concurrent writes"}
    (directory/"overview-results.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)