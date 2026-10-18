from datetime import date

from app.seed_timeline import timeline_checks


def rows():
    return [dict(year=year, is_recurring=False, gifts=count, dollars=10000,
                 retained_dollars=10000, gross_year_end=3000, year_end=3000)
            for year, count in ((2021, 90000), (2022, 100), (2023, 110),
                                (2024, 115), (2025, 120), (2026, 1))]


def test_timeline_uses_reference_bounds_not_observed_maximum():
    verdict = timeline_checks(rows(), date(2026, 10, 18))
    assert verdict["full_years"] == [2022, 2023, 2024, 2025]
    assert not verdict["errors"]
    assert verdict["largest_count_changes"]["one-time"] == (2022, 2023, 10)


def test_timeline_rejects_missing_years_seasonality_and_volume_collapse():
    annual = rows()
    annual[3]["year_end"] = annual[3]["gross_year_end"] = 140
    annual[4]["gifts"] = 3
    verdict = timeline_checks(annual, date(2026, 10, 18))
    assert any("2024 Nov-Dec" in error for error in verdict["errors"])
    assert any("2024→2025" in error for error in verdict["errors"])
    assert timeline_checks([r for r in annual if r["year"] != 2023],
                           date(2026, 10, 18))["errors"]


def test_timeline_thresholds_are_inclusive():
    annual = rows()
    for row, count in zip(annual[1:5], (100, 125, 100, 125)):
        row["gifts"] = count
        row["year_end"] = row["gross_year_end"] = 2500 if count == 100 else 3500
    assert not timeline_checks(annual, date(2026, 10, 18))["errors"]
    annual[4]["gifts"] = 126
    assert timeline_checks(annual, date(2026, 10, 18))["errors"]
