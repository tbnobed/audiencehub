"""Synthetic giving model only; no database, importer or identity dependencies.

Annual campaign/season budgets deliberately calibrate *dollars*, not gift counts.
Log-normal draws within those budgets preserve a long tail and channel effects.
Only one year is buffered, so this also works for the large CSV generator.
"""

import calendar
import math
import random
from collections import defaultdict
from datetime import date
from typing import Iterator, NamedTuple


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


def _one_time_date(rng: random.Random, year: int, campaign: str) -> date:
    if campaign == "Year-End Giving":
        # 60% lands in Nov/Dec; the advance appeal starts in September.
        month = rng.choices((9, 10, 11, 12), (0.15, 0.25, 0.22, 0.38))[0]
    else:
        # The remaining campaigns put 10% in Nov/Dec. Together with Year-End:
        # 40% * 60% + 60% * 10% = 30% of one-time dollars.
        month = rng.choices(
            range(1, 13),
            (1.2, 1.2, 1.3, 1.3, 1.2, 0.65, 0.55, 0.65, 1.2, 1.75, 0.5, 0.5),
        )[0]
    if month == 12 and rng.random() < 0.55:
        day = rng.randint(25, 31)
    else:
        day = rng.randint(1, 24 if month == 12 else calendar.monthrange(year, month)[1])
    return date(year, month, day)


def generate_giving(profiles: int, density: float, random_seed: int) -> Iterator[SeedGift]:
    """Five calendar years, 45% annual donor retention, 4.5% dollar growth.

    Each active partner gives twice (plus density-dependent extra gifts).
    Twelve percent have a monthly plan with a stable amount/day for that year.
    Annual active cohorts grow 4.5%; lapsed partners can reactivate later, but
    never accidentally inflate adjacent-year retention. Tiny fixtures cannot
    represent every campaign or a statistically meaningful retention rate.
    """
    rng = random.Random(random_seed ^ 0xBEEF)
    previous: set[int] = set()
    previous_recurring: set[int] = set()
    population = set(range(profiles))
    one_time = tuple(item for item in CAMPAIGN_MODEL if item[0] != "Monthly Partner Program")
    # Density changes gift frequency, not the requested typical gift size.
    # E[floor(Exponential(mean=d))] = 1 / (exp(1/d) - 1).
    gifts_per_donor = 2 + 1 / math.expm1(1 / density) + 0.12 * 12
    small_gifts_per_donor = 2 + 1 / math.expm1(2) + 0.12 * 12
    annual_budget_per_donor = 450 * gifts_per_donor / small_gifts_per_donor
    for year in range(2020, 2025):
        active_count = min(profiles, max(1, round(profiles * 0.18 * 1.045 ** (year - 2020))))
        retained_count = round(len(previous) * 0.45)
        # Monthly partners are stickier, but still churn.
        sticky_count = min(retained_count, round(len(previous_recurring) * 0.70))
        retained = set(rng.sample(sorted(previous_recurring), sticky_count))
        retained.update(rng.sample(sorted(previous - previous_recurring),
                                   retained_count - sticky_count))
        available = population - previous
        new_count = min(active_count - len(retained), len(available))
        active = retained | set(rng.sample(sorted(available), new_count))
        if len(active) < active_count:
            # Only relevant to tiny fixtures where rounded counts exhaust the pool.
            active.update(rng.sample(sorted(population - active), active_count - len(active)))
        recurring_count = round(len(active) * 0.12)
        recurring = set(sorted(retained & previous_recurring)[:recurring_count])
        recurring.update(rng.sample(sorted(active - recurring), recurring_count - len(recurring)))
        rows: list[SeedGift] = []
        totals: dict[tuple[str, bool], float] = defaultdict(float)
        for person in sorted(active):
            if person in recurring:
                amount = rng.lognormvariate(math.log(50), 0.50)
                day = rng.randint(1, 28)
                for month in range(1, 13):
                    rows.append(SeedGift(person, date(year, month, day), amount,
                                         "Monthly Partner Program", "online", True))
                    totals[("Monthly Partner Program", False)] += amount
            for _ in range(2 + int(rng.expovariate(1 / density))):
                campaign, _, size = rng.choices(
                    one_time, weights=[share / size for _, share, size in one_time],
                )[0]
                channel_index = rng.choices(range(len(CHANNELS)), CHANNEL_WEIGHTS)[0]
                amount = rng.lognormvariate(math.log(55 * size), 1.65) * CHANNEL_SIZE[channel_index]
                gift_date = _one_time_date(rng, year, campaign)
                rows.append(SeedGift(person, gift_date, amount, campaign,
                                     CHANNELS[channel_index], False))
                totals[(campaign, gift_date.month >= 11)] += amount
        # Same budget per active donor means cohort growth drives gradual revenue
        # growth instead of a handful of unusually large random draws driving it.
        # Redistribute absent campaigns for tiny fixtures only; never lose dollars.
        budgets: dict[tuple[str, bool], float] = {}
        for campaign, share, _ in CAMPAIGN_MODEL:
            seasonal_share = (0.0 if campaign == "Monthly Partner Program" else
                              0.60 if campaign == "Year-End Giving" else 0.10)
            for year_end, fraction in ((False, 1 - seasonal_share), (True, seasonal_share)):
                if totals[(campaign, year_end)]:
                    budgets[(campaign, year_end)] = share * fraction
        present_share = sum(budgets.values())
        factors = {key: len(active) * annual_budget_per_donor * share / present_share / totals[key]
                   for key, share in budgets.items()}
        for row in rows:
            key = (row.campaign, not row.recurring and row.gift_date.month >= 11)
            yield row._replace(amount=round(row.amount * factors[key], 2))
        previous, previous_recurring = active, recurring
