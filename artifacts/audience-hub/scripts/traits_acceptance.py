#!/usr/bin/env python3
"""Compare computed traits with a deliberately straightforward Python reference.

Reads only the isolated acceptance database. RFM quintiles are calculated from
all donors' raw gifts, then the requested seeded profile sample is compared
against profile_traits. No aggregate trait values are used as reference inputs.
"""

import argparse
import os
import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import create_engine, text


FIELDS = (
    "gift_count_total", "ltv_total", "gift_amount_12m", "gift_count_12m",
    "first_gift_date", "last_gift_date", "largest_gift_amount",
    "avg_gift_amount", "is_recurring_active", "days_since_last_gift",
    "donor_status", "rfm_recency", "rfm_frequency", "rfm_monetary",
    "rfm_score", "event_count_30d", "last_event_at", "video_views_30d",
    "last_engagement_channel", "source_keys",
)
CENT = Decimal("0.01")


def _quintile(index: int, population: int) -> int:
    """PostgreSQL ntile(5) for a zero-based position."""
    base, extra = divmod(population, 5)
    if index < (base + 1) * extra:
        return index // (base + 1) + 1
    return extra + (index - (base + 1) * extra) // base + 1


def compare(database_url: str, as_of: date, sample_size: int, seed: int) -> dict:
    engine = create_engine(database_url)
    cutoff = as_of + timedelta(days=1)
    with engine.connect() as db:
        transaction = db.begin()
        db.exec_driver_sql("SET TRANSACTION READ ONLY")
        try:
            active_ids = list(db.execute(text("""
                SELECT id FROM profiles
                WHERE merged_into_id IS NULL AND NOT is_deleted ORDER BY id
            """)).scalars())
            sample = random.Random(seed).sample(active_ids, min(sample_size, len(active_ids)))
            selected = set(sample)
            sources = dict(db.execute(text("SELECT id, key FROM sources")).all())
            donor_stats = defaultdict(lambda: [0, Decimal(0), None])
            sample_gifts = defaultdict(list)
            gift_rows = db.execution_options(stream_results=True).execute(text("""
                SELECT g.profile_id, g.id, g.amount, g.gift_date, g.is_recurring, g.source_id
                FROM gifts g JOIN profiles p ON p.id=g.profile_id
                WHERE p.merged_into_id IS NULL AND NOT p.is_deleted
                  AND (g.gift_date IS NULL OR g.gift_date <= :as_of)
            """), {"as_of": as_of})
            while batch := gift_rows.fetchmany(10_000):
                for profile_id, gift_id, amount, gift_date, recurring, source_id in batch:
                    stats = donor_stats[profile_id]
                    stats[0] += 1
                    stats[1] += amount
                    if gift_date is not None and (stats[2] is None or gift_date > stats[2]):
                        stats[2] = gift_date
                    if profile_id in selected:
                        sample_gifts[profile_id].append(
                            (gift_date, gift_id, amount, recurring, sources[source_id]))
            gift_rows.close()

            ranked = {}
            donor_ids = list(donor_stats)
            rankings = (
                sorted(donor_ids, key=lambda pid: (
                    (as_of - donor_stats[pid][2]).days
                    if donor_stats[pid][2] is not None else 2**31, pid)),
                sorted(donor_ids, key=lambda pid: (-donor_stats[pid][0], pid)),
                sorted(donor_ids, key=lambda pid: (-donor_stats[pid][1], pid)),
            )
            for ranking in rankings:
                scores = {
                    pid: 6 - _quintile(index, len(ranking))
                    for index, pid in enumerate(ranking)
                }
                ranked[len(ranked)] = scores

            events = defaultdict(list)
            event_rows = db.execute(text("""
                SELECT e.profile_id, e.id, e.name, e.occurred_at, s.key
                FROM events e JOIN sources s ON s.id=e.source_id
                WHERE e.profile_id=ANY(:ids) AND e.occurred_at < :cutoff
            """), {"ids": sample, "cutoff": cutoff})
            for profile_id, event_id, name, occurred_at, source in event_rows:
                events[profile_id].append((occurred_at, event_id, name, source))
            event_rows.close()

            source_keys = defaultdict(set)
            for profile_id, key in db.execute(text("""
                SELECT DISTINCT sr.profile_id, s.key
                FROM source_records sr JOIN sources s ON s.id=sr.source_id
                WHERE sr.profile_id=ANY(:ids)
                UNION
                SELECT DISTINCT g.profile_id, s.key
                FROM gifts g JOIN sources s ON s.id=g.source_id
                WHERE g.profile_id=ANY(:ids)
                UNION
                SELECT DISTINCT e.profile_id, s.key
                FROM events e JOIN sources s ON s.id=e.source_id
                WHERE e.profile_id=ANY(:ids)
            """), {"ids": sample}):
                source_keys[profile_id].add(key)

            actual = {
                row["profile_id"]: dict(row)
                for row in db.execute(text("""
                    SELECT * FROM profile_traits WHERE profile_id=ANY(:ids)
                """), {"ids": sample}).mappings()
            }
            mismatches = defaultdict(list)
            distribution = defaultdict(int)
            for profile_id in sample:
                gifts = sample_gifts[profile_id]
                dated = sorted((g for g in gifts if g[0]), key=lambda g: (g[0], g[1]))
                first = dated[0][0] if dated else None
                last = dated[-1][0] if dated else None
                age = (as_of - last).days if last else None
                count = len(gifts)
                total = sum((g[2] for g in gifts), Decimal(0))
                recent = [g for g in gifts if g[0] and g[0] >= as_of - timedelta(days=364)]
                gap = (dated[-1][0] - dated[-2][0]).days if len(dated) >= 2 else None
                if last is None:
                    status = "prospect"
                elif age <= 365 and first >= as_of - timedelta(days=365):
                    status = "new"
                elif age <= 365 and gap is not None and gap > 730:
                    status = "reactivated"
                elif age <= 365:
                    status = "active"
                elif age <= 730:
                    status = "lapsing"
                else:
                    status = "lapsed"
                distribution[status] += 1
                recent_events = [
                    e for e in events[profile_id]
                    if e[0] >= datetime.combine(
                        as_of - timedelta(days=29), time.min, tzinfo=timezone.utc)
                ]
                engagements = [
                    (datetime.combine(g[0], time.min, tzinfo=timezone.utc), g[1], g[4])
                    for g in dated
                ] + [(e[0], e[1], e[3]) for e in events[profile_id]]
                recency = ranked[0].get(profile_id)
                frequency = ranked[1].get(profile_id)
                monetary = ranked[2].get(profile_id)
                expected = {
                    "gift_count_total": count,
                    "ltv_total": total.quantize(CENT),
                    "gift_amount_12m": sum((g[2] for g in recent), Decimal(0)).quantize(CENT),
                    "gift_count_12m": len(recent),
                    "first_gift_date": first,
                    "last_gift_date": last,
                    "largest_gift_amount": max((g[2] for g in gifts), default=None),
                    "avg_gift_amount": (
                        (total / count).quantize(CENT, rounding=ROUND_HALF_UP)
                        if count else None
                    ),
                    "is_recurring_active": any(
                        g[3] and g[0] and g[0] >= as_of - timedelta(days=44) for g in gifts
                    ),
                    "days_since_last_gift": age,
                    "donor_status": status,
                    "rfm_recency": recency,
                    "rfm_frequency": frequency,
                    "rfm_monetary": monetary,
                    "rfm_score": (
                        f"{recency}{frequency}{monetary}" if recency is not None else None
                    ),
                    "event_count_30d": len(recent_events),
                    "last_event_at": max((e[0] for e in events[profile_id]), default=None),
                    "video_views_30d": sum(e[2] == "Video Watched" for e in recent_events),
                    "last_engagement_channel": (
                        max(engagements, key=lambda item: (item[0], item[1]))[2]
                        if engagements else None
                    ),
                    "source_keys": sorted(source_keys[profile_id]),
                }
                observed = actual.get(profile_id, {})
                for field in FIELDS:
                    if observed.get(field) != expected[field]:
                        mismatches[field].append(profile_id)
            transaction.rollback()
            return {
                "sample_size": len(sample), "as_of": as_of.isoformat(),
                "donors_ranked": len(donor_ids),
                "sample_donor_status": dict(sorted(distribution.items())),
                "mismatches": {
                    field: {"count": len(ids), "profile_ids": ids[:5]}
                    for field, ids in sorted(mismatches.items())
                },
            }
        except Exception:
            transaction.rollback()
            raise
        finally:
            engine.dispose()


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--sample", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20250308)
    args = parser.parse_args()
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    print(json.dumps(compare(url, args.as_of, args.sample, args.seed), indent=2))