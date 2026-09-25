"""Overview contracts backed by explicitly refreshed, isolated rollup fixtures."""
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from test_consent_ingestion_pg import db
from app.dashboards import api
from app.dashboards.overview import delta, month_shift
from app.dashboards.rollups import refresh_dashboard_rollups


def test_delta_and_leap_dates():
    assert delta(0, 0)["delta_label"] == "—"
    assert delta(2, 0)["delta_label"] == "New"
    assert delta(2, 1)["change_pct"] == 100
    assert month_shift(date(2024, 2, 29), -12) == date(2023, 2, 28)


def test_default_range_with_no_historical_data(db):
    refresh_dashboard_rollups(db)
    db.commit()
    result = api._dashboard("overview", None, None, db)
    assert result["range"]["to"] == date.today().isoformat()
    assert result["range"]["from"] == date.today().replace(day=1).isoformat()
    assert result["overview"]["kpis"]["giving"]["value"] == 0
    assert result["overview"]["kpis"]["retention_yoy"]["denominator"] == 0
    assert len(result["overview"]["kpis"]["giving"]["sparkline"]) == 12


def test_real_statement_deadline_is_explicit(db, monkeypatch):
    from fastapi import HTTPException
    from app.dashboards import cache
    monkeypatch.setattr(cache, "DEADLINE_SECONDS", .2)
    with pytest.raises(HTTPException) as error:
        with cache.deadline(db):
            api._scalar(db, "SELECT pg_sleep(1)")
    assert error.value.status_code == 504


def test_resolved_population_giving_and_period_contract(db):
    source = db.execute(text("INSERT INTO sources(key,name,kind) VALUES ('overview-test','Test','crm') RETURNING id")).scalar_one()
    ids = [db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one() for _ in range(4)]
    db.execute(text("UPDATE profiles SET merged_into_id=:winner WHERE id=:loser"), {"winner": ids[0], "loser": ids[1]})
    db.execute(text("UPDATE profiles SET is_deleted=true WHERE id=:id"), {"id": ids[2]})
    for i, pid in enumerate([ids[0], ids[0], ids[1], ids[2], None]):
        db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,campaign)
             VALUES (:source,:external,:pid,:amount,'2025-03-15','Spring')"""),
                   {"source": source, "external": str(i), "pid": pid, "amount": 10 if pid == ids[0] else 10000})
    refresh_dashboard_rollups(db)
    db.commit()
    result = api._dashboard("overview", date(2025, 3, 10), date(2025, 3, 20), db)
    o = result["overview"]
    assert o["kpis"]["giving"]["value"] == 20
    assert o["kpis"]["active_partners"]["value"] == 1
    assert o["kpis"]["average_gift"]["value"] == 10
    assert o["kpis"]["giving"]["delta_label"] == "New"
    assert o["stats"]["profiles"]["value"] == 2
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


def test_retention_exclusive_statuses_and_role_safe_shell(db):
    source = db.execute(text("INSERT INTO sources(key,name,kind) VALUES ('status-test','Test','crm') RETURNING id")).scalar_one()
    histories = [
        ["2023-01-01", "2024-06-01", "2025-06-01"],
        ["2025-06-02"], ["2020-01-01", "2025-06-03"],
        ["2023-08-01"], ["2020-02-01"], [],
    ]
    for index, history in enumerate(histories):
        pid = db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
        for n, day in enumerate(history):
            db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date)
                VALUES (:s,:x,:p,10,:day)"""), {"s": source, "x": f"{index}-{n}", "p": pid, "day": date.fromisoformat(day)})
    refresh_dashboard_rollups(db)
    db.commit()
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
    (date(2025, 1, 1), date(2025, 1, 30)), (date(2025, 1, 1), date(2025, 3, 31)),
    (date(2024, 3, 1), date(2025, 2, 28)), (date(2025, 1, 1), date(2025, 6, 17)),
    (date(2023, 7, 1), date(2024, 6, 30)), (date(2024, 2, 29), date(2024, 3, 2)),
])
def test_empty_ranges_are_zero_filled_and_prior_aligned(db, start, end):
    refresh_dashboard_rollups(db)
    db.commit()
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