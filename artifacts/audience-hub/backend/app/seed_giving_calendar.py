"""Calendar budgets shared by all audit donors, before recipient assignment."""
from bisect import bisect_left, bisect_right
from collections import defaultdict


def bounded_budget(total, weights, minimum, allocate):
    """Apportion an exact budget, honoring mandatory gifts without adding rows."""
    locked = {}
    while True:
        remaining = total - sum(locked.values())
        if remaining < 0:
            raise ValueError("Mandatory gifts exceed the calendar budget")
        available = [0 if i in locked else weight for i, weight in enumerate(weights)]
        result = allocate(remaining, available, range(len(weights)))
        for i, value in locked.items():
            result[i] = value
        violations = [i for i in range(len(weights)) if result[i] < minimum[i]]
        if not violations:
            if sum(result) != total:
                raise ValueError("Calendar budget has no eligible dates")
            return result
        for i in violations:
            locked[i] = minimum[i]


class CalendarPool:
    """One-time date slots; logarithmic constrained sampling without replacement."""

    def __init__(self, days, cumulative, total, fixed, recent_cutoff, allocate, *, eligible_end=None):
        self.days = days
        months = defaultdict(list)
        weights, previous = [], 0
        for i, (day, cumulative_weight) in enumerate(zip(days, cumulative)):
            weight = cumulative_weight - previous
            previous = cumulative_weight
            # The explicit recent-person frequency budget supersedes the old
            # 190/day target. No incidental recent gifts may escape that budget.
            eligible = day < recent_cutoff and (eligible_end is None or day <= eligible_end)
            weights.append(round(weight * 10**12) if eligible else 0)
            months[day.year, day.month].append(i)
        groups = list(months.values())
        minima = [fixed[day] for day in days]
        monthly = bounded_budget(total,
                                 [sum(weights[i] for i in group) for group in groups],
                                 [sum(minima[i] for i in group) for group in groups], allocate)
        self.counts = [0] * len(days)
        for group, quota in zip(groups, monthly):
            daily = bounded_budget(quota, [weights[i] for i in group],
                                   [minima[i] for i in group], allocate)
            for i, count in zip(group, daily):
                self.counts[i] = count - minima[i]
        self.tree = [0] * (len(days) + 1)
        for i, count in enumerate(self.counts):
            self._add(i, count)

    def _add(self, index, value):
        index += 1
        while index < len(self.tree):
            self.tree[index] += value
            index += index & -index

    def _prefix(self, index):
        total = 0
        while index:
            total += self.tree[index]
            index -= index & -index
        return total

    def take(self, start, end, rng):
        left, right = bisect_left(self.days, start), bisect_right(self.days, end)
        low, high = self._prefix(left), self._prefix(right)
        if high <= low:
            raise ValueError(f"Insufficient calendar slots for required gifts in {start}..{end}")
        target, index = rng.randrange(low, high), 0
        bit = 1 << (len(self.days).bit_length() - 1)
        while bit:
            candidate = index + bit
            if candidate < len(self.tree) and self.tree[candidate] <= target:
                target -= self.tree[candidate]
                index = candidate
            bit >>= 1
        self.counts[index] -= 1
        self._add(index, -1)
        return self.days[index]

    def remaining(self):
        for day, count in zip(self.days, self.counts):
            for _ in range(count):
                yield day
