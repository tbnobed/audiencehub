"""Deterministic gift plans inside the accepted audit audience cohorts.

No identity fields/cohort selectors are generated here. Memory is O(donors),
not O(gifts); only the current person's gift dates are materialized.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
import hashlib
from itertools import accumulate
import math
import random


@lru_cache(maxsize=128)
def seasonal_days(start, end):
    # 30% November/December exposure, summer dip, 55% of December in its
    # final week. Partial boundary years are naturally truncated, never padded.
    month_weights = (.08, .08, .08, .08, .08, .045, .035, .045, .07, .105, .13, .17)
    days, weights = [], []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        length = calendar.monthrange(day.year, day.month)[1]
        fraction = (.55 / 7 if day.day >= 25 else .45 / 24) if day.month == 12 else 1 / length
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
            if i >= self.basic and i not in self.recent_rank and i >= targets["Giving Tuesday 2024"]:
                self.singles.add(i)

        lengths, initial_recent = [], 0
        for i in range(self.g):
            gifts = self.base_gifts(i)
            lengths.append(len(gifts))
            initial_recent += sum(g.day >= window.as_of - timedelta(days=30) for g in gifts)
        recent_target = round(profiles * 2957 * 30 / 7830000) if self.recent else 0
        recent_weights = [int(i in self.recent_rank and i not in self.singles) for i in range(self.g)]
        self.extra_recent = allocate(max(0, recent_target - initial_recent), recent_weights, order)
        self.recent_donations = initial_recent + sum(self.extra_recent)
        self.counts = [max(lengths[i], 1 if i in self.singles else 2) + self.extra_recent[i]
                       for i in range(self.g)]
        target = round(profiles * 345000 / 500000) if self.g else 0
        extra_weights = []
        for i in range(self.g):
            bucket = self.rank[i] % 10000
            extra_weights.append(0 if i in self.singles or i < self.recurring else
                                 30 if bucket >= 9900 else 5 if bucket >= 9200 else
                                 1 if bucket >= 7000 else 0)
        if self.g and not sum(extra_weights):
            extra_weights = [int(i not in self.singles) for i in range(self.g)]
        extras = allocate(max(0, target - sum(self.counts)), extra_weights, order)
        self.counts = [count + extras[i] for i, count in enumerate(self.counts)]
        self.donations = sum(self.counts)
        capacity = [min(self.counts[i] - lengths[i] - self.extra_recent[i],
                        max(0, self.counts[i] - 2)) if i not in self.singles else 0
                    for i in range(self.g)]
        return_target = min(round(self.donations * 27 / 2957), sum(capacity))
        self.returns = allocate(return_target, capacity, order)
        self.return_count = sum(self.returns)
        self.recent_returns = min(self.return_count, round(profiles * 27 * 30 / 7830000))
        self.return_ordinal = 0
        self.amount_ordinal = 0
        # Permute all gift ordinals, yielding exact tail quotas spread across
        # donors/dates rather than concentrating them at the start of the file.
        self.stride = 104729
        while self.donations and math.gcd(self.stride, self.donations) != 1:
            self.stride += 2
        self.offset = seed % max(1, self.donations)
        self.major_count = round(self.donations * .0005)
        self.tail_count = round(self.donations * .015)

    def rng(self, i):
        return random.Random(self.rank[i])

    def base_gifts(self, i):
        rng, w = self.rng(i), self.w
        if i in self.recent_rank:
            anchor = w.end - timedelta(days=self.recent_rank[i] % 30)
        elif i < self.active_end:
            anchor = (w.as_of - timedelta(days=721 + i % 10) if i < self.subscription_end
                      else seasonal_date(rng, w.as_of - timedelta(days=730),
                                         w.as_of - timedelta(days=31)))
        else:
            anchor = seasonal_date(rng, w.start, w.as_of - timedelta(days=731))
        gifts = [PlannedGift(anchor, "history", "SYN-YE" if anchor.month >= 11 else "SYN-GEN")]
        if i < self.recurring:
            month = w.start.replace(day=1)
            while month < w.as_of.replace(day=1):
                day = month.replace(day=calendar.monthrange(month.year, month.month)[1])
                if w.start <= day <= w.end:
                    gifts.append(PlannedGift(day, f"monthly-{day:%Y%m}", is_recurring=True))
                month = (month.replace(year=month.year + 1, month=1) if month.month == 12
                         else month.replace(month=month.month + 1))
        if i < self.t["Giving Tuesday 2024"] and w.start <= self.gt <= w.end:
            gifts.append(PlannedGift(self.gt, "gt", self.gt_codes[i % 4]))
        return gifts

    def gifts(self, i):
        rng, w = self.rng(i), self.w
        result = self.base_gifts(i)
        for j in range(self.extra_recent[i]):
            day = w.end - timedelta(days=rng.randrange(30))
            result.append(PlannedGift(day, f"recent-{j}"))
        end = w.as_of - timedelta(days=731 if i >= self.active_end else
                                 721 if i < self.subscription_end and i not in self.recent_rank else 31)
        historical = self.counts[i] - len(result)
        for j in range(historical):
            day = seasonal_date(rng, w.start, end)
            refund_day = None
            if j < self.returns[i]:
                if self.return_ordinal < self.recent_returns:
                    refund_day = w.end - timedelta(days=self.return_ordinal % 30)
                else:
                    bound = min(w.as_of - timedelta(days=31), day + timedelta(days=30))
                    refund_day = day + timedelta(days=rng.randrange((bound - day).days + 1))
                self.return_ordinal += 1
            result.append(PlannedGift(day, f"history-{j}",
                                     "SYN-YE" if day.month >= 11 else "SYN-GEN", refund_day))
        return result

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
