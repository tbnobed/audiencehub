"""Shared UTC, calendar-anniversary bounds for synthetic seed timestamps."""

import calendar
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone


@dataclass(frozen=True)
class SeedWindow:
    as_of: date
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.as_of - self.start).days

    @property
    def start_at(self) -> datetime:
        return datetime.combine(self.start, time.min, timezone.utc)

    @property
    def end_at(self) -> datetime:
        return datetime.combine(self.as_of, time.min, timezone.utc) - timedelta(seconds=1)

    def random_date(self, rng: random.Random) -> date:
        return self.start + timedelta(days=rng.randrange(self.days))

    def random_datetime(self, rng: random.Random) -> datetime:
        return self.start_at + timedelta(seconds=rng.randrange(self.days * 86400))


def seed_window(as_of: date | None = None) -> SeedWindow:
    """Five calendar years ending yesterday; Feb 29 clamps to Feb 28."""
    if as_of is None:
        as_of = datetime.now(timezone.utc).date()
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise ValueError("as_of must be a date")
    start = as_of.replace(
        year=as_of.year - 5,
        day=min(as_of.day, calendar.monthrange(as_of.year - 5, as_of.month)[1]),
    )
    return SeedWindow(as_of, start, as_of - timedelta(days=1))
