def test_daily_trait_schedule_does_not_reenqueue_after_job_completion(monkeypatch):
    from types import SimpleNamespace
    from app.jobs import scheduler

    calls = []

    class ScheduleDb:
        scheduled = False

        def get(self, model, key):
            return SimpleNamespace(task=key[0]) if self.scheduled else None

        def execute(self, statement, params=None):
            if params and "key" in params:
                return None  # PostgreSQL advisory lock
            self.scheduled = True
            return SimpleNamespace(scalar_one_or_none=lambda: "traits.nightly_full")

    monkeypatch.setattr(
        scheduler, "enqueue",
        lambda db, job_type, payload, dedupe_key: (
            calls.append(job_type) or SimpleNamespace(id=1)
        ),
    )
    db = ScheduleDb()
    assert scheduler.schedule_once(
        db, "traits.nightly_full", "2026-09-24", "traits.recompute", {"mode": "full"}
    )
    assert not scheduler.schedule_once(
        db, "traits.nightly_full", "2026-09-24", "traits.recompute", {"mode": "full"}
    )
    assert calls == ["traits.recompute"]
from datetime import date
import sys
from types import ModuleType

import pytest

from app.traits.engine import _RECOMPUTE_SQL, recompute_traits
from app.traits.registry import TRAITS, TRAITS_BY_KEY


def _reference_donor_status(gift_dates: list[date], as_of: date) -> str:
    dated = sorted(day for day in gift_dates if day <= as_of)
    if not dated:
        return "prospect"
    most_recent = dated[-1]
    days_since = (as_of - most_recent).days
    if days_since <= 365 and (as_of - dated[0]).days <= 365:
        return "new"
    if days_since <= 365:
        if len(dated) > 1 and (most_recent - dated[-2]).days > 730:
            return "reactivated"
        return "active"
    if days_since <= 730:
        return "lapsing"
    return "lapsed"


def _reference_ntile(values: list[tuple[int, int]], *, descending: bool) -> dict[int, int]:
    """Mirror PostgreSQL NTILE(5), including deterministic tie-breaking by id."""
    ordered = sorted(values, key=lambda item: ((-item[1] if descending else item[1]), item[0]))
    count = len(ordered)
    base, remainder = divmod(count, 5)
    result = {}
    offset = 0
    for tile in range(1, 6):
        bucket_size = base + (1 if tile <= remainder else 0)
        for profile_id, _ in ordered[offset:offset + bucket_size]:
            result[profile_id] = 6 - tile
        offset += bucket_size
    return result


def test_donor_status_boundaries_365_366_730_731_days():
    as_of = date(2025, 12, 30)
    assert _reference_donor_status([date(2024, 12, 30)], as_of) == "new"
    assert _reference_donor_status([date(2024, 12, 29)], as_of) == "lapsing"
    assert _reference_donor_status([date(2023, 12, 31)], as_of) == "lapsing"
    assert _reference_donor_status([date(2023, 12, 30)], as_of) == "lapsed"
    assert _reference_donor_status([], as_of) == "prospect"
    assert "days_since_last_gift <= 365" in str(_RECOMPUTE_SQL)
    assert "days_since_last_gift <= 730" in str(_RECOMPUTE_SQL)


def test_reactivated_requires_prior_gift_gap_over_730_days():
    as_of = date(2025, 12, 31)
    assert _reference_donor_status(
        [date(2022, 12, 30), date(2025, 12, 31)], as_of
    ) == "reactivated"
    assert _reference_donor_status(
        [date(2024, 1, 1), date(2025, 12, 31)], as_of
    ) == "active"


def test_rfm_quintiles_rank_highest_frequency_as_five_and_handle_small_populations():
    scores = _reference_ntile([(profile_id, profile_id) for profile_id in range(1, 11)],
                              descending=True)
    assert [scores[index] for index in range(10, 0, -1)] == [5, 5, 4, 4, 3, 3, 2, 2, 1, 1]
    one_donor = _reference_ntile([(42, 3)], descending=True)
    assert one_donor == {42: 5}


def test_trait_registry_covers_documented_profile_trait_fields():
    documented = {
        "gift_count_total", "ltv_total", "gift_amount_12m", "gift_count_12m",
        "first_gift_date", "last_gift_date", "largest_gift_amount", "avg_gift_amount",
        "is_recurring_active", "days_since_last_gift", "donor_status", "rfm_recency",
        "rfm_frequency", "rfm_monetary", "rfm_score", "event_count_30d",
        "last_event_at", "video_views_30d", "last_engagement_channel", "source_keys",
        "computed_at",
    }
    assert set(TRAITS_BY_KEY) == documented
    assert len(TRAITS) == len(TRAITS_BY_KEY)
    assert all(item.label and item.type and item.description and item.sql for item in TRAITS)
    statement = str(_RECOMPUTE_SQL).upper()
    assert "INSERT INTO PROFILE_TRAITS" in statement
    assert "ON CONFLICT (PROFILE_ID) DO UPDATE" in statement
    assert "NTILE(5)" in statement


def test_recompute_passes_the_pinned_as_of_date_to_one_set_based_upsert():
    class Result:
        rowcount = 7

    class Recorder:
        def __init__(self):
            self.executions = []

        def execute(self, statement, params=None):
            self.executions.append((str(statement), params))
            return Result()

    pinned = date(2020, 2, 29)
    db = Recorder()
    assert recompute_traits(db, as_of=pinned, write_snapshot=False) == 7
    upsert, params = next(
        (statement, parameters) for statement, parameters in db.executions
        if "INSERT INTO profile_traits" in statement
    )
    assert params == {"as_of": pinned, "profile_ids": None}
    assert "CAST(:as_of AS date)" in upsert
    assert "INSERT INTO profile_traits" in upsert


def test_dashboard_cache_is_not_invalidated_when_trait_recompute_fails(monkeypatch):
    from app.jobs import handlers
    from app.traits import engine

    invalidations = []

    class FailingSession:
        def __init__(self, unused_engine):
            pass

        def execute(self, *_args, **_kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    dashboards = ModuleType("app.dashboards.api")
    dashboards.invalidate_cache = lambda: invalidations.append(True)
    monkeypatch.setitem(sys.modules, "app.dashboards.api", dashboards)
    monkeypatch.setattr("app.db.engine", object())
    monkeypatch.setattr("sqlalchemy.orm.Session", FailingSession)
    monkeypatch.setattr(
        engine, "recompute_traits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("recompute failed")),
    )

    with pytest.raises(RuntimeError, match="recompute failed"):
        handlers.run("traits.recompute", {"mode": "full"})
    assert invalidations == []


def test_dirty_worker_locks_before_selecting_dirty_rows(monkeypatch):
    from types import SimpleNamespace
    from app.jobs import handlers

    class Recorder:
        def __init__(self):
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, _params=None):
            self.statements.append(str(statement))
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

        def commit(self):
            pass

    recorder = Recorder()
    monkeypatch.setattr("app.db.engine", object())
    monkeypatch.setattr("sqlalchemy.orm.Session", lambda _engine: recorder)
    handlers.run("traits.recompute", {"mode": "dirty"})
    assert "pg_advisory_xact_lock" in recorder.statements[0]
    assert "FOR UPDATE SKIP LOCKED" in recorder.statements[1]