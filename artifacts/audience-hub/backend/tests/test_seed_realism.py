import csv
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median

import pytest

from app.seed_giving import generate_giving
from app.seed_report import measure_giving


COLUMNS = (
    "external_id", "contact_external_id", "email", "phone", "amount",
    "gift_date", "campaign", "channel", "is_recurring", "recurring_plan_id",
)
AS_OF = date(2025, 1, 1)


@pytest.fixture(scope="module")
def small_report(tmp_path_factory):
    """Independent CSV reader checks a full 50k small-density giving population."""
    path = tmp_path_factory.mktemp("giving") / "gifts.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        for number, gift in enumerate(generate_giving(50_000, 0.5, 20250308, as_of=AS_OF)):
            writer.writerow({
                "external_id": f"gift-{number}", "contact_external_id": f"p-{gift.person_index}",
                "amount": f"{gift.amount:.2f}", "gift_date": str(gift.gift_date),
                "campaign": gift.campaign, "channel": gift.channel,
                "is_recurring": str(gift.recurring).lower(),
                "recurring_plan_id": (f"p-{gift.person_index}-{gift.gift_date.year}"
                                      if gift.recurring else ""),
            })
    return measure_giving(path)


def test_campaign_dollar_shares_and_distinct_averages(small_report):
    campaigns = small_report["campaigns"]
    assert 10 <= len(campaigns) <= 15
    assert 30 <= campaigns["Year-End Giving"]["dollar_share_pct"] <= 35
    assert 19 <= campaigns["Monthly Partner Program"]["dollar_share_pct"] <= 21
    shares = [row["dollar_share_pct"] for row in campaigns.values()]
    assert shares == sorted(shares, reverse=True)
    assert shares[-1] < 2
    assert len({row["average_gift"] for row in campaigns.values()}) == len(campaigns)
    assert campaigns["Legacy Impact"]["average_gift"] > campaigns["Food for Families"]["average_gift"]


def test_lognormal_long_tail_and_channel_sizes(small_report):
    sizes = small_report["gift_sizes"]
    assert 45 <= sizes["median"] <= 55
    assert 1 <= sizes["1000_or_more_pct"] <= 2
    assert 1 <= sizes["10000_or_more"] < small_report["valid_gifts"] * 0.001
    assert sizes["maximum"] > 25000  # no old $25k truncation
    channels = small_report["channels"]
    assert channels["mail"]["average_gift"] < channels["online"]["average_gift"]
    assert channels["major_gift"]["gifts"] / small_report["valid_gifts"] < 0.001
    assert channels["major_gift"]["average_gift"] > 5000
    assert (small_report["one_time_channels"]["online"]["average_gift"]
            > small_report["one_time_channels"]["mail"]["average_gift"] * 1.5)


def test_annual_seasonality_growth_and_flat_monthly_plans(small_report):
    for stats in small_report["annual"].values():
        assert 29.9 <= stats["one_time_nov_dec_share_pct"] <= 30.1
        assert 26 <= stats["nov_dec_share_pct"] <= 29  # recurring dilutes seasonality
        assert stats["last_week_share_of_one_time_dec_pct"] > 45
        months = stats["one_time_monthly_cents"]
        assert sum(months[5:8]) / 3 < sum(months[:5]) / 5 * 0.8
        assert len(set(stats["recurring_monthly_cents"])) == 1
        if stats["dollar_growth_pct"] is not None:
            assert 3 <= stats["dollar_growth_pct"] <= 6
    assert small_report["recurring_plans"] > 0
    assert small_report["complete_flat_recurring_plans"] == small_report["recurring_plans"]


def test_retention_has_churn_and_replacement_not_100_percent(small_report):
    first_year = min(small_report["annual"])
    for year, stats in small_report["annual"].items():
        if year == first_year:
            assert stats["retention_pct"] is None  # no fabricated 2019 cohort
            continue
        assert 40 <= stats["retention_pct"] <= 50
        assert 0 < stats["retained_donors"] < stats["prior_year_donors"]
        assert stats["donors"] > 0  # Revenue growth is not donor-count growth.


def test_giving_is_deterministic_and_tiny_fixtures_work():
    first = list(generate_giving(100, 0.5, 19, as_of=AS_OF))
    assert first == list(generate_giving(100, 0.5, 19, as_of=AS_OF))
    assert first != list(generate_giving(100, 0.5, 20, as_of=AS_OF))
    assert all(g.amount > 0 for g in first)
    assert list(generate_giving(1, 0.5, 19, as_of=AS_OF))


@pytest.mark.parametrize("density", [0.5, 1.0, 2.0])
def test_density_does_not_destroy_typical_size_or_long_tail(density):
    amounts = [gift.amount for gift in generate_giving(10_000, density, 19, as_of=AS_OF)]
    assert 42 <= median(amounts) <= 58
    assert 0.01 <= sum(amount >= 1000 for amount in amounts) / len(amounts) <= 0.02


@pytest.mark.parametrize("as_of", [date(2026, 7, 18), date(2024, 2, 29), AS_OF])
def test_current_lifecycles_and_monthly_partners(as_of):
    from app.seed_dates import seed_window

    window = seed_window(as_of)
    gifts = list(generate_giving(10_000, 0.5, 19, as_of=as_of))
    dates = defaultdict(list)
    recurring = defaultdict(list)
    for gift in gifts:
        assert window.start <= gift.gift_date <= window.end
        dates[gift.person_index].append(gift.gift_date)
        if gift.recurring:
            recurring[gift.person_index].append(gift.gift_date)
    statuses = Counter(
        "active" if (as_of - max(days)).days <= 365 else
        "lapsing" if (as_of - max(days)).days <= 730 else "lapsed"
        for days in dates.values())
    assert len(dates) == 7000  # Prospects are not part of the giver denominator.
    for status, expected in (("active", 0.40), ("lapsing", 0.12), ("lapsed", 0.48)):
        assert statuses[status] / len(dates) == pytest.approx(expected, abs=0.005)
    assert any(min(days) >= as_of - timedelta(days=90) for days in dates.values())
    assert any((as_of - max(days)).days <= 365 and len(days) >= 2
               and (sorted(days)[-1] - sorted(days)[-2]).days > 730 for days in dates.values())
    last_month = as_of.replace(day=1) - timedelta(days=1)
    assert recurring
    assert all(any((day.year, day.month) == (last_month.year, last_month.month)
                   for day in days) for days in recurring.values())
    assert all((as_of - max(days)).days <= 365 for days in recurring.values())


def test_partial_years_do_not_fabricate_complete_year_growth(tmp_path):
    as_of = date(2026, 7, 18)
    path = tmp_path / "partial.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        for number, gift in enumerate(generate_giving(10_000, 0.5, 19, as_of=as_of)):
            writer.writerow(dict(external_id=str(number), contact_external_id=str(gift.person_index),
                                 amount=f"{gift.amount:.2f}", gift_date=str(gift.gift_date),
                                 campaign=gift.campaign, channel=gift.channel,
                                 is_recurring=str(gift.recurring).lower()))
    annual = measure_giving(path)["annual"]
    assert len(annual) == 6
    # Only consecutive complete calendar years have the modeled annual growth.
    for year in range(2023, 2026):
        assert 3 <= annual[year]["dollar_growth_pct"] <= 6
        assert 29.9 <= annual[year]["one_time_nov_dec_share_pct"] <= 30.1
        assert 40 <= annual[year]["retention_pct"] <= 50
    assert annual[2026]["one_time_nov_dec_share_pct"] == 0
    assert annual[2026]["dollar_growth_pct"] < 0


def _write_fixture(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def test_report_is_independent_hand_computed_and_excludes_only_known_messiness(tmp_path):
    path = tmp_path / "gifts.csv"

    def row(gift, donor, amount, when):
        return dict(external_id=gift, contact_external_id=donor, amount=amount,
                    gift_date=when, campaign="Appeal", channel="mail", is_recurring="false")

    rows = [
        row("a", "one", "50.00", "2020-01-01"),
        row("b", "two", "150.00", "Nov 15, 2020"),
        row("c", "one", "100.00", "12/28/2021"),
        row("d", "three", "300.00", "2021-07-01"),
    ]
    _write_fixture(path, rows + [rows[0], {"amount": "not-a-number"}])
    report = measure_giving(path)
    assert report["valid_gifts"] == 4
    assert report["duplicate_rows_excluded"] == report["invalid_sentinel_rows_excluded"] == 1
    assert report["total_dollars"] == 600
    assert report["gift_sizes"]["median"] == 125
    assert report["campaigns"]["Appeal"]["dollar_share_pct"] == 100
    assert report["annual"][2020]["one_time_nov_dec_share_pct"] == 75
    assert report["annual"][2021]["one_time_nov_dec_share_pct"] == 25
    assert report["annual"][2021]["prior_year_donors"] == 2
    assert report["annual"][2021]["retained_donors"] == 1
    assert report["annual"][2021]["retention_pct"] == 50
    assert report["annual"][2021]["dollar_growth_pct"] == 100


def test_report_fails_loud_on_unexpected_bad_data_and_conflicting_duplicates(tmp_path):
    path = tmp_path / "gifts.csv"
    good = dict(external_id="a", contact_external_id="p", amount="50.00",
                gift_date="2020-01-01", campaign="Appeal", channel="mail", is_recurring="false")
    _write_fixture(path, [good, dict(good, amount="60.00")])
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        measure_giving(path)
    for change in ({"amount": "NaN"}, {"amount": "0.00"}, {"gift_date": "bad-date"},
                   {"amount": "50.001"}, {"contact_external_id": ""}):
        _write_fixture(path, [dict(good, **change)])
        with pytest.raises(ValueError, match="Invalid gift CSV line"):
            measure_giving(path)
