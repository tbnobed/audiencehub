"""Bounded synthetic lifecycles with calendar-year dollar calibration.

Complete calendar years have 4.5% dollar growth. Boundary years are partial:
their budgets include only the seasonal/monthly exposure inside the window,
so their raw totals must not be compared as if they were complete years.
"""

import calendar
import math
import random
from collections import defaultdict
from datetime import date, timedelta
from functools import lru_cache
from itertools import accumulate
from typing import Iterator, NamedTuple

from app.seed_dates import SeedWindow, seed_window


# Name, share of ALL dollars, relative one-time gift size.
CAMPAIGN_MODEL = (
    ("Year-End Giving", 0.32, 1.15),
    ("Monthly Partner Program", 0.20, 1.0),
    ("Spring Outreach", 0.112, 1.05),
    ("Global Missions", 0.088, 0.95),
    ("Broadcast Ministry", 0.072, 1.25),
    ("Community Relief", 0.056, 0.90),
    ("Easter Hope", 0.040, 0.85),
    ("Summer Youth", 0.032, 0.80),
    ("Bible Translation", 0.028, 1.40),
    ("Food for Families", 0.020, 0.75),
    ("Prayer Outreach", 0.016, 0.70),
    ("Legacy Impact", 0.016, 1.70),
)
CAMPAIGNS = tuple(item[0] for item in CAMPAIGN_MODEL)
CHANNELS = ("mail", "online", "phone", "event", "major_gift")
CHANNEL_WEIGHTS = (0.43, 0.43, 0.10, 0.0395, 0.0005)
CHANNEL_SIZE = (0.65, 1.25, 0.90, 1.05, 60.0)


class SeedGift(NamedTuple):
    person_index: int
    gift_date: date
    amount: float
    campaign: str
    channel: str
    recurring: bool


def seed_giver_indices(profiles: int, random_seed: int) -> set[int]:
    """Leave a genuine nongiver population, independent of timestamp draws."""
    order = list(range(profiles))
    random.Random(random_seed ^ 0xC040).shuffle(order)
    return set(order[:max(1, round(profiles * 0.70))])


def _cohorts(profiles: int, random_seed: int, as_of: date):
    """40% recent, 12% lapsing, 48% lapsed among all synthetic givers.

    Complete calendar-year cohorts retain approximately 40–50%. Recent donor
    counts need not grow at the same rate as revenue: budgets, not headcounts,
    control revenue growth. Recurring donors are deliberately sticky.
    """
    rng = random.Random(random_seed ^ 0xC040)
    # Calendar years straddle two lifecycle buckets except at New Year.
    # Reduce anniversary retention in proportion to that overlap so complete
    # calendar-year retention remains near 45%, not artificially inflated.
    year_fraction = (as_of.timetuple().tm_yday - 1) / (366 if calendar.isleap(as_of.year) else 365)
    historical_retention = 0.45 - 0.65 * year_fraction * (1 - year_fraction)
    population = seed_giver_indices(profiles, random_seed)
    size = len(population)
    recent_count = max(1, round(size * 0.40))
    lapsing_count = round(size * 0.12)
    prior_count = min(size, max(lapsing_count, round(lapsing_count / 0.55)))
    counts = [max(1, round(size * share)) for share in (0.31, 0.32, 0.33)]
    counts.extend((prior_count, recent_count))
    recurring = set(rng.sample(sorted(population), min(recent_count, round(size * 0.035))))
    previous: set[int] = set()
    seen: set[int] = set()
    cohorts: list[set[int]] = []
    for index, count in enumerate(counts):
        retained_count = (min(count, max(len(recurring), round(len(previous) * historical_retention)))
                          if index else 0)
        if index == 4:
            # Exactly the intended previous-year-only population remains lapsing.
            retained_count = max(0, len(previous) - lapsing_count)
        retained = recurring & previous
        retained.update(rng.sample(sorted(previous - retained),
                                   min(len(previous - retained), retained_count - len(retained))))
        active = retained | recurring
        unseen = population - seen - active
        if index == 4:
            active.update(unseen)
            # Reactivations skip both preceding buckets, ensuring a >730-day gap.
            candidates = seen - previous - cohorts[2] - active
        else:
            candidates = unseen
        needed = max(0, count - len(active))
        active.update(rng.sample(sorted(candidates), min(needed, len(candidates))))
        if len(active) < count:
            candidates = population - active - previous
            active.update(rng.sample(sorted(candidates), min(count - len(active), len(candidates))))
        cohorts.append(active)
        seen.update(active)
        previous = active
    return cohorts, recurring


def _periods(window: SeedWindow) -> list[tuple[date, date]]:
    starts = [window.start]
    for offset in (4, 3):
        year = window.as_of.year - offset
        starts.append(window.as_of.replace(
            year=year, day=min(window.as_of.day, calendar.monthrange(year, window.as_of.month)[1])))
    starts.extend((window.as_of - timedelta(days=730), window.as_of - timedelta(days=365),
                   window.as_of))
    return [(left, right - timedelta(days=1)) for left, right in zip(starts, starts[1:])]


def _date_weights(campaign: str) -> tuple[float, ...]:
    if campaign == "Year-End Giving":
        return (0, 0, 0, 0, 0, 0, 0, 0, 0.15, 0.25, 0.22, 0.38)
    return (1.2, 1.2, 1.3, 1.3, 1.2, 0.65, 0.55, 0.65, 1.2, 1.75, 0.5, 0.5)


@lru_cache(maxsize=128)
def _date_distribution(start: date, end: date, campaign: str):
    choices, weights = [], []
    for year in range(start.year, end.year + 1):
        for month, weight in enumerate(_date_weights(campaign), 1):
            days = calendar.monthrange(year, month)[1]
            left, right = max(start, date(year, month, 1)), min(end, date(year, month, days))
            if left <= right and weight:
                for day in range(left.day, right.day + 1):
                    choices.append(date(year, month, day))
                    weights.append(weight * ((0.55 / 7 if day >= 25 else 0.45 / 24)
                                             if month == 12 else 1 / days))
    return choices, list(accumulate(weights))


def _one_time_date(rng: random.Random, start: date, end: date, campaign: str) -> date:
    """Sample only available dates, preserving monthly and late-December weights."""
    choices, weights = _date_distribution(start, end, campaign)
    if not choices:
        return start + timedelta(days=rng.randrange((end - start).days + 1))
    return rng.choices(choices, cum_weights=weights)[0]


@lru_cache(maxsize=256)
def _exposure(window: SeedWindow, year: int, campaign: str, year_end: bool) -> float:
    """Fraction of the full calendar-year seasonal bucket present in the window."""
    months = [month for month in range(1, 13)
              if campaign == "Monthly Partner Program" or (month >= 11) == year_end]
    weights = ((1,) * 12 if campaign == "Monthly Partner Program" else _date_weights(campaign))
    denominator = sum(weights[month - 1] for month in months)
    numerator = 0.0
    for month in months:
        days = calendar.monthrange(year, month)[1]
        for day in range(1, days + 1):
            when = date(year, month, day)
            if window.start <= when <= window.end:
                fraction = ((0.55 / 7 if day >= 25 else 0.45 / 24)
                            if month == 12 and campaign != "Monthly Partner Program" else 1 / days)
                numerator += weights[month - 1] * fraction
    return numerator / denominator if denominator else 0.0


def _gift_years(profiles: int, density: float, random_seed: int,
                window: SeedWindow, rng: random.Random):
    """Buffer at most two neighboring years rather than the full five-year history."""
    cohorts, recurring = _cohorts(profiles, random_seed, window.as_of)
    periods = _periods(window)
    one_time = tuple(item for item in CAMPAIGN_MODEL if item[0] != "Monthly Partner Program")
    rows_by_year: dict[int, list[SeedGift]] = defaultdict(list)
    plans = []
    for person in sorted(recurring):
        amount = rng.lognormvariate(math.log(50), 0.50)
        day = rng.randint(1, 28)
        plans.append((person, amount, day))

    def finish_year(year):
        rows = rows_by_year.pop(year, [])
        for person, amount, day in plans:
            for month in range(1, 13):
                when = date(year, month, day)
                if window.start <= when <= window.end:
                    rows.append(SeedGift(
                        person, when, amount, "Monthly Partner Program", "online", True))
        return rows

    next_year = window.start.year
    historical = set().union(*cohorts[:4])
    reactivated = cohorts[4] & historical - cohorts[3] - cohorts[2]
    for period_index, (active, (start, end)) in enumerate(zip(cohorts, periods)):
        for person in sorted(active):
            count = (1 if period_index == 4 and person in reactivated else
                     2 + int(rng.expovariate(1 / density)))
            for _ in range(count):
                campaign, _, size = rng.choices(
                    one_time, weights=[share / size for _, share, size in one_time])[0]
                channel_index = rng.choices(range(len(CHANNELS)), CHANNEL_WEIGHTS)[0]
                amount = rng.lognormvariate(math.log(55 * size), 1.65) * CHANNEL_SIZE[channel_index]
                when = _one_time_date(rng, start, end, campaign)
                rows_by_year[when.year].append(SeedGift(
                    person, when, amount, campaign, CHANNELS[channel_index], False))
        # A later anniversary bucket cannot contribute to a completed calendar year.
        while next_year < end.year:
            yield next_year, finish_year(next_year)
            next_year += 1
    while next_year <= window.end.year:
        yield next_year, finish_year(next_year)
        next_year += 1


def generate_giving(profiles: int, density: float, random_seed: int,
                    as_of: date | None = None) -> Iterator[SeedGift]:
    """Generate five calendar years ending yesterday, including realistic lifecycles."""
    window = seed_window(as_of)
    rng = random.Random(random_seed ^ 0xBEEF)
    gifts_per_donor = 2 + 1 / math.expm1(1 / density) + 0.12 * 12
    baseline = 2 + 1 / math.expm1(2) + 0.12 * 12
    annual_budget = profiles * 0.195 * 450 * gifts_per_donor / baseline
    for year, rows in _gift_years(profiles, density, random_seed, window, rng):
        if not rows:
            continue
        totals: dict[tuple[str, bool], float] = defaultdict(float)
        for row in rows:
            totals[(row.campaign, not row.recurring and row.gift_date.month >= 11)] += row.amount
        budgets = {}
        for campaign, share, _ in CAMPAIGN_MODEL:
            seasonal_share = (0.0 if campaign == "Monthly Partner Program" else
                              0.60 if campaign == "Year-End Giving" else 0.10)
            for year_end, fraction in ((False, 1 - seasonal_share), (True, seasonal_share)):
                key = campaign, year_end
                if totals[key]:
                    budgets[key] = share * fraction * _exposure(window, year, campaign, year_end)
        # If a rare campaign bucket is absent, redistribute its dollars within
        # that same seasonal bucket, not across Nov/Dec versus the rest of year.
        for year_end in (False, True):
            present_season = sum(value for (campaign, seasonal), value in budgets.items()
                                 if campaign != "Monthly Partner Program" and seasonal == year_end)
            target_season = sum(
                share * (season if year_end else 1 - season)
                * _exposure(window, year, campaign, year_end)
                for campaign, share, _ in CAMPAIGN_MODEL if campaign != "Monthly Partner Program"
                for season in (0.60 if campaign == "Year-End Giving" else 0.10,))
            if present_season:
                for key in budgets:
                    if key[0] != "Monthly Partner Program" and key[1] == year_end:
                        budgets[key] *= target_season / present_season
        # Tiny fixtures redistribute absent buckets; production-sized data represents all.
        full_exposure = sum(
            share * fraction * _exposure(window, year, campaign, year_end)
            for campaign, share, _ in CAMPAIGN_MODEL
            for year_end, fraction in (
                (False, 1 - (0 if campaign == "Monthly Partner Program" else
                             0.60 if campaign == "Year-End Giving" else 0.10)),
                (True, 0 if campaign == "Monthly Partner Program" else
                 0.60 if campaign == "Year-End Giving" else 0.10)))
        present = sum(budgets.values())
        budget = annual_budget * 1.045 ** (year - window.start.year) * full_exposure
        factors = {key: budget * share / present / totals[key] for key, share in budgets.items()}
        for row in rows:
            key = row.campaign, not row.recurring and row.gift_date.month >= 11
            yield row._replace(amount=max(0.01, round(row.amount * factors[key], 2)))
