"""Deterministic gift plans inside the accepted audit audience cohorts.

No identity fields/cohort selectors are generated here. Calendar slots are
budgeted globally, then assigned within the accepted recipient constraints.
"""
import calendar
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
import hashlib
from itertools import accumulate
import math
import random

from app.seed_giving_calendar import CalendarPool
from app.seed_timeline import timeline_checks


@lru_cache(maxsize=128)
def seasonal_days(start, end):
    # 30% November/December exposure, summer dip, 55% of December in its
    # final week. Partial boundary years are naturally truncated, never padded.
    month_weights = (.08, .08, .08, .08, .08, .045, .035, .045, .07, .105, .13, .17)
    days, weights = [], []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        length = calendar.monthrange(day.year, day.month)[1]
        november = date(day.year, 11, 1)
        giving_tuesday = november + timedelta(days=(3 - november.weekday()) % 7 + 26)
        if day.month == 12:
            fraction = (.55 / 7 if day.day >= 25 else
                        .07 if day == giving_tuesday else
                        .38 / 23 if giving_tuesday.month == 12 else .45 / 24)
        elif day.month == 11 and giving_tuesday.month == 11:
            fraction = .07 if day == giving_tuesday else .93 / (length - 1)
        else:
            fraction = 1 / length
        days.append(day)
        weights.append(month_weights[day.month - 1] * fraction * 1.04 ** (day.year - end.year))
    return days, list(accumulate(weights))


def seasonal_date(rng, start, end):
    end = max(start, end)
    days, weights = seasonal_days(start, end)
    return rng.choices(days, cum_weights=weights)[0]


@dataclass
class PlannedGift:
    day: date
    suffix: str
    code: str = "SYN-GEN"
    refund_day: date | None = None
    is_recurring: bool = False
    protected: bool = True
    amount_cents: int = 0


def allocate(total, weights, order):
    """Exact integer quotas, without per-person independent rounding drift."""
    denominator = sum(weights)
    result = [0] * len(weights)
    if not denominator or not total:
        return result
    for i, weight in enumerate(weights):
        result[i] = total * weight // denominator
    remaining = total - sum(result)
    eligible = [i for i in order if weights[i]]
    # Largest remainders, deterministic cohort-independent tie break.
    eligible.sort(key=lambda i: total * weights[i] % denominator, reverse=True)
    for i in eligible[:remaining]:
        result[i] += 1
    return result


class AuditGiving:
    def __init__(self, profiles, targets, window, seed, gt, gt_codes):
        self.t, self.w, self.seed, self.gt, self.gt_codes = targets, window, seed, gt, gt_codes
        self.g = targets["RFM TBN All Donor Reference"]
        self.recurring = targets["Basic Recurring Donors"]
        self.basic = targets["Basic Donors"]
        self.subscription_end = self.basic + targets["TBN Plus Premium"]
        self.active_end = self.g - targets["RFM Lapsed Donors"]
        self.recent = targets["Donors Past 30 Days"]
        self.rank = [int.from_bytes(hashlib.blake2b(
            f"{seed}:giving:{i}".encode(), digest_size=8).digest(), "big") for i in range(self.g)]
        order = sorted(range(self.g), key=self.rank.__getitem__)
        self.recent_rank = {}
        app_recent = min(self.recent, targets["TBN App Donors"])
        for i in range(self.g):
            rank = (i if i < app_recent else app_recent + i - self.subscription_end
                    if self.subscription_end <= i < self.subscription_end + self.recent - app_recent
                    else None)
            if rank is not None:
                self.recent_rank[i] = rank
        fixed = set(range(self.recurring, min(
            self.g, self.recurring + targets["Basic One-Time Donors"])))
        self.singles = set(fixed)
        for i in order:
            if len(self.singles) >= round(self.g * .35):
                break
            # Other basic donors must not become rfm_raw_frequency=1.
            if (i >= self.basic and i >= self.active_end and i not in self.recent_rank
                    and i >= targets["Giving Tuesday 2024"]):
                self.singles.add(i)

        self.plans = [[] for _ in range(self.g)]
        recent_cutoff = window.as_of - timedelta(days=30)
        for i in range(self.g):
            if i in self.recent_rank:
                self.plans[i].append(PlannedGift(
                    window.end - timedelta(days=self.recent_rank[i] % 30), "history"))
            elif i < self.active_end and i < self.subscription_end:
                # This narrow intersection is required by the unchanged
                # TBN+ 720-day and nonzero-RFM 730-day audience memberships.
                self.plans[i].append(PlannedGift(
                    window.as_of - timedelta(days=721 + i % 10), "history"))
            if i < self.recurring:
                month = window.start.replace(day=1)
                while month < window.as_of.replace(day=1):
                    day = month.replace(day=calendar.monthrange(month.year, month.month)[1])
                    if window.start <= day:
                        self.plans[i].append(PlannedGift(
                            day, f"monthly-{day:%Y%m}", is_recurring=True))
                    month = day + timedelta(days=1)
            if i < targets["Giving Tuesday 2024"] and window.start <= gt <= window.end:
                self.plans[i].append(PlannedGift(gt, "gt", gt_codes[i % 4]))
        initial_recent = sum(g.day >= recent_cutoff for gifts in self.plans for g in gifts)
        # Keep the exact recent people; the newer 1.2–1.6/person requirement
        # replaces the previous 2,957/day population scaling.
        recent_target = max(initial_recent, round(self.recent * 1.4))
        recent_weights = [int(i in self.recent_rank and i not in self.singles) for i in range(self.g)]
        self.extra_recent = allocate(max(0, recent_target - initial_recent), recent_weights, order)
        for i in range(self.g):
            rng = self.rng(i)
            for j in range(self.extra_recent[i]):
                self.plans[i].append(PlannedGift(
                    window.end - timedelta(days=rng.randrange(30)), f"recent-{j}"))
        self.recent_donations = initial_recent + sum(self.extra_recent)
        minimum = sum(max(len(self.plans[i]), 1 if i in self.singles else 2)
                      for i in range(self.g))
        self.donations = max(minimum, round(profiles * 345000 / 500000)) if self.g else 0
        self._calendar_plan(recent_cutoff, order)
        self.counts = [len(gifts) for gifts in self.plans]
        self.amount_ordinal = 0
        # Permute all gift ordinals, yielding exact tail quotas spread across
        # donors/dates rather than concentrating them at the start of the file.
        self.stride = 104729
        while self.donations and math.gcd(self.stride, self.donations) != 1:
            self.stride += 2
        self.offset = seed % max(1, self.donations)
        self.major_count = round(self.donations * .0005)
        self.tail_count = round(self.donations * .015)
        for i, gifts in enumerate(self.plans):
            rng = random.Random(self.rank[i] ^ 0xA510)
            for gift in gifts:
                gift.amount_cents = self.amount(rng)
        self._refunds(profiles, order)
        self._balance_year_end()
        self.timeline = self._timeline()
        if profiles >= 50000 and self.timeline["errors"]:
            raise ValueError("Calendar plan failed acceptance: " + "; ".join(self.timeline["errors"]))

    def rng(self, i):
        return random.Random(self.rank[i])

    def _calendar_plan(self, recent_cutoff, order):
        w = self.w
        fixed = Counter(g.day for gifts in self.plans for g in gifts if not g.is_recurring)
        recurring_count = sum(g.is_recurring for gifts in self.plans for g in gifts)
        days, cumulative = seasonal_days(w.start, w.end)
        eligible_end = max((w.as_of - timedelta(
            days=731 if i >= self.active_end else
            721 if i < self.subscription_end and i not in self.recent_rank else 31)
            for i in range(self.g) if i not in self.singles), default=w.start)
        pool = CalendarPool(days, cumulative, self.donations - recurring_count,
                            fixed, recent_cutoff, allocate, eligible_end=eligible_end)
        # Earliest-deadline cohorts receive their minimum gifts first. Their
        # minimum dates are drawn from GLOBAL slots, never independently
        # normalized over a three-year donor-specific truncated window.
        required_order = sorted(order, key=lambda i: (
            0 if i >= self.active_end else 1 if i < self.subscription_end
            and i not in self.recent_rank else 2))
        for i in required_order:
            gifts, rng = self.plans[i], self.rng(i)
            lower = w.start
            upper = w.as_of - timedelta(days=731 if i >= self.active_end else
                                       721 if i < self.subscription_end
                                       and i not in self.recent_rank else 31)
            if not gifts and i < self.active_end:
                lower = w.as_of - timedelta(days=730)
            needed = (1 if i in self.singles else 2) - len(gifts)
            for j in range(max(0, needed)):
                day = pool.take(lower, upper, rng)
                gifts.append(PlannedGift(day, "history" if not gifts else "minimum"))
        deadlines = {}
        for i in order:
            if i not in self.singles:
                deadlines[i] = (max(g.day for g in self.plans[i]) if i >= self.active_end else
                                w.as_of - timedelta(days=721 if i < self.subscription_end
                                                    and i not in self.recent_rank else 31))
        people = sorted(deadlines, key=lambda i: (deadlines[i], self.rank[i]))
        stops = [deadlines[i] for i in people]
        weights = [30 if self.rank[i] % 10000 >= 9900 else
                   5 if self.rank[i] % 10000 >= 9200 else 1 for i in people]
        cumulative_people = [0, *accumulate(weights)]
        rng = random.Random(self.seed ^ 0xC41E)
        for day in pool.remaining():
            left = bisect_left(stops, day)
            if left == len(people):
                raise ValueError("Calendar volume has no eligible non-single giver")
            chosen = bisect_right(cumulative_people,
                                  rng.randrange(cumulative_people[left], cumulative_people[-1])) - 1
            i = people[chosen]
            self.plans[i].append(PlannedGift(day, f"history-{len(self.plans[i])}", protected=False))
        for gifts in self.plans:
            for gift in gifts:
                if gift.code == "SYN-GEN" and gift.day.month >= 11:
                    gift.code = "SYN-YE"

    def _refunds(self, profiles, order):
        capacity = [sum(not g.protected for g in gifts)
                    for gifts in self.plans]
        self.returns = allocate(min(round(self.donations * 27 / 2957), sum(capacity)),
                                capacity, order)
        self.return_count = sum(self.returns)
        self.recent_returns = min(self.return_count, round(profiles * 27 * 30 / 7830000))
        self.return_ordinal = 0
        for i, gifts in enumerate(self.plans):
            rng, remaining = self.rng(i), self.returns[i]
            for gift in gifts:
                if remaining and not gift.protected:
                    if self.return_ordinal < self.recent_returns:
                        gift.refund_day = self.w.end - timedelta(days=self.return_ordinal % 30)
                    else:
                        end = min(self.w.as_of - timedelta(days=31), gift.day + timedelta(days=30))
                        gift.refund_day = gift.day + timedelta(days=rng.randrange((end-gift.day).days + 1))
                    self.return_ordinal += 1
                    remaining -= 1

    def _balance_year_end(self):
        """Swap amounts only, within a year; preserve all dates and amount quotas."""
        years = defaultdict(list)
        for gifts in self.plans:
            for gift in gifts:
                if not gift.is_recurring:
                    years[gift.day.year].append(gift)
        for year, gifts in years.items():
            if not (self.w.start <= date(year, 1, 1) and date(year, 12, 31) <= self.w.end):
                continue
            retained = [g for g in gifts if g.refund_day is None]
            total = sum(g.amount_cents for g in retained)
            gross = sum(g.amount_cents for g in gifts)
            returned_ye = sum(g.amount_cents for g in gifts if g.refund_day and g.day.month >= 11)
            low = max(math.ceil(.25 * total), math.ceil(.25 * gross) - returned_ye)
            high = min(math.floor(.35 * total), math.floor(.35 * gross) - returned_ye)
            year_end = sum(g.amount_cents for g in retained if g.day.month >= 11)
            if low <= year_end <= high:
                continue
            adding = year_end < low
            receivers = sorted((g for g in retained if (g.day.month >= 11) == adding),
                               key=lambda g: g.amount_cents)
            donors = sorted((g for g in retained if (g.day.month >= 11) != adding),
                            key=lambda g: g.amount_cents, reverse=True)
            # Disjoint pairs; moving toward the interval cannot change the
            # yearly total, gift count, dates, tail counts or return pairing.
            for receiver, donor in zip(receivers, donors):
                delta = donor.amount_cents - receiver.amount_cents
                if delta <= 0:
                    break
                candidate = year_end + (delta if adding else -delta)
                if (adding and candidate > high) or (not adding and candidate < low):
                    continue
                receiver.amount_cents, donor.amount_cents = donor.amount_cents, receiver.amount_cents
                year_end = candidate
                if low <= year_end <= high:
                    break
            # Tiny cohorts can have a single immutable tail amount larger than
            # the entire acceptable interval. Preserve the distribution, and
            # surface this through the normal timeline diagnostics instead of
            # inventing/rescaling amounts. Production-sized plans fail closed.
            if total and not low <= year_end <= high and high - low >= 5000000:
                raise ValueError(f"Cannot balance {year} one-time dollar seasonality without changing quotas")

    def _timeline(self):
        grouped = defaultdict(lambda: dict(gifts=0, dollars=0, retained_dollars=0,
                                           year_end=0, gross_year_end=0))
        for gifts in self.plans:
            for gift in gifts:
                row = grouped[gift.day.year, gift.is_recurring]
                row["gifts"] += 1
                row["dollars"] += gift.amount_cents
                if gift.day.month >= 11:
                    row["gross_year_end"] += gift.amount_cents
                if gift.refund_day is None:
                    row["retained_dollars"] += gift.amount_cents
                    if gift.day.month >= 11:
                        row["year_end"] += gift.amount_cents
        rows = [dict(year=year, is_recurring=recurring, **values)
                for (year, recurring), values in grouped.items()]
        return timeline_checks(rows, self.w.as_of)

    def gifts(self, i):
        return self.plans[i]

    def amount(self, rng):
        rank = (self.amount_ordinal * self.stride + self.offset) % max(1, self.donations)
        self.amount_ordinal += 1
        if rank < self.major_count:
            dollars = 50000 if rank == 0 else math.exp(rng.uniform(math.log(10000), math.log(50000)))
        elif rank < self.tail_count:
            dollars = math.exp(rng.uniform(math.log(1000), math.log(9999.99)))
        else:
            dollars = max(5, min(999.99, rng.lognormvariate(math.log(49.4), .60)))
        return round(dollars * 100)
