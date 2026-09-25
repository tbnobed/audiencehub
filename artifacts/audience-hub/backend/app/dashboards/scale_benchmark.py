"""Reproducible synthetic scale evidence on real, migrated disposable PostgreSQL."""
import json
import os
import time
from datetime import date


def measure(directory, population):
    if ("benchmark@/benchmark?host=/tmp/kinship-bench-" not in os.environ.get("DATABASE_URL", "")
            or os.environ.get("APP_ENV") != "test"):
        raise RuntimeError("Dashboard benchmark requires the disposable PostgreSQL harness")
    if population < 1:
        raise ValueError("--profiles must be positive")
    from sqlalchemy import text, event
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.dashboards.api import _CSV_CHARTS
    from app.dashboards.rollups import refresh_dashboard_rollups
    with Session(engine) as db:
        db.execute(text("INSERT INTO sources(key,name,kind) VALUES('scale','Scale','crm')"))
        db.execute(text("""INSERT INTO profiles(email)
          SELECT 'scale'||i||'@example.org' FROM generate_series(1,:n) i"""), {"n": population})
        db.execute(text("""
          INSERT INTO source_records(source_id,external_id,profile_id,email_norm,resolved_at,raw_hash)
          SELECT (SELECT id FROM sources WHERE key='scale'),'p-'||id,id,email,now(),md5(id::text)
          FROM profiles"""))
        # Four dated gifts/profile with overlapping years and same-day repeat
        # donors; 2025 current giving is exactly population*30, donors population.
        db.execute(text("""
          INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,campaign,fund,channel,is_recurring)
          SELECT (SELECT id FROM sources WHERE key='scale'),p.id||'-'||v.n,p.id,10,
            date '2024-01-01'+(p.id%365)::int+v.offset_days,
            'Campaign '||(p.id%8),'Fund '||(p.id%3),'email',p.id%10=0
          FROM profiles p CROSS JOIN (VALUES(1,0),(2,366),(3,366),(4,366)) v(n,offset_days)
        """))
        db.execute(text("""
          INSERT INTO consents(profile_id,channel,status,source_id)
          SELECT id,'email','opted_in',(SELECT id FROM sources WHERE key='scale') FROM profiles"""))
        db.commit()
        counts = dict(db.execute(text("""SELECT
          (SELECT count(*) FROM profiles) AS profiles,(SELECT count(*) FROM gifts) AS gifts,
          (SELECT count(*) FROM source_records) AS source_records,
          (SELECT count(*) FROM consents) AS consents""")).mappings().one())
        start = time.perf_counter()
        refresh_dashboard_rollups(db, as_of=date(2025, 12, 31))
        db.commit()
        refresh_seconds = time.perf_counter()-start
    import re
    def forbid_raw(conn, cursor, statement, parameters, context, executemany):
        if re.search(r"\b(?:FROM|JOIN)\s+(?:public\.)?(?:gifts|events|source_records)\b", statement, re.I):
            raise AssertionError("Raw dashboard query: "+statement)
    event.listen(engine, "before_cursor_execute", forbid_raw)
    timings = []
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    try:
        login = client.post("/auth/dev-login", json={"role": "admin", "name": "Scale benchmark"})
        assert login.status_code == 200, login.text
        endpoints = ["overview", "giving", "retention", "engagement", "sources", "data-health"]
        endpoints += ["overview/"+card for card in
                      ("kpis", "giving-by-month", "needs-attention", "campaigns", "partner-status", "email", "attention")]
        endpoints += ["overview/primary"]
        endpoints += [f"{dashboard}/{chart}/csv" for dashboard, charts in _CSV_CHARTS.items()
                      for chart in sorted(charts)]
        for name in endpoints:
            with Session(engine) as db:
                db.execute(text("DELETE FROM dashboard_cache"))
                db.commit()
            measurements = []
            for _ in range(2):
                start = time.perf_counter()
                response = client.get("/api/dashboards/"+name,
                                      params={"from": "2025-01-01", "to": "2025-12-31"})
                assert response.status_code == 200, response.text
                payload = response.json() if not name.endswith("/csv") else None
                measurements.append(round((time.perf_counter()-start)*1000, 2))
            if name == "overview":
                kpis = payload["overview"]["kpis"]
                assert kpis["giving"]["value"] == population*30
                assert kpis["active_partners"]["value"] == population
                assert kpis["retention_yoy"]["value"] == 100
                assert payload["overview"]["stats"]["email_opted_in"]["value"] == population
            timings.append({"endpoint": "/api/dashboards/"+name,
                            "cold_ms": measurements[0], "warm_ms": measurements[1]})
            print(f"{name:30} cold={measurements[0]:9.2f}ms warm={measurements[1]:8.2f}ms "
                  f"{'PASS' if max(measurements)<300 else 'FAIL'}", flush=True)
    finally:
        client.close()
        event.remove(engine, "before_cursor_execute", forbid_raw)
    report = {"counts": counts, "refresh_seconds": refresh_seconds, "timings": timings,
              "target_ms": 300, "target_pass": all(max(r["cold_ms"],r["warm_ms"])<300 for r in timings),
              "checks": "Exact giving, distinct donors, prior-year retention, authoritative consent; raw-table SQL guard",
              "scope": "Real PostgreSQL; synthetic four-gift/profile fixture. Authenticated ASGI TestClient "
                       "after real dev login: includes routing, signed session/auth, DB sessions/teardown, "
                       "JSON/CSV serialization, middleware; excludes real network/browser. "
                       "Repeated temporal histories compress exactly; this is not a production-history diversity guarantee."}
    (directory/"dashboards.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2), flush=True)