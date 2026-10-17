"""Independent CSV measurement for seed acceptance; never imports the model.

Usage: python -m app.seed_report /isolated/seed/giving_platform_gifts.csv
Only the deliberately blank-identity invalid sentinel is excluded. Duplicate
external IDs must have identical contents. Unexpected malformed data fails loud.
"""

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median


def _date(value: str):
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized gift date: {value!r}")


def measure_giving(path: str | Path) -> dict:
    """Measure unique, valid CSV gifts in integer cents, grouped independently.

    Retention uses contact_external_id, NOT email, names or gift IDs. This is
    pre-import person-level evidence, not an identity-resolved database report.
    """
    seen: dict[str, dict] = {}
    campaigns = defaultdict(lambda: [0, 0])
    channels = defaultdict(lambda: [0, 0])
    one_time_channels = defaultdict(lambda: [0, 0])
    cohorts: dict[int, set[str]] = defaultdict(set)
    annual = defaultdict(lambda: {
        "gift_count": 0, "cents": 0, "one_time_cents": 0,
        "nov_dec_cents": 0, "one_time_nov_dec_cents": 0,
        "one_time_dec_cents": 0, "one_time_last_week_dec_cents": 0,
        "monthly_cents": [0] * 12, "recurring_monthly_cents": [0] * 12,
        "one_time_monthly_cents": [0] * 12,
    })
    amounts = []
    plans = defaultdict(list)
    duplicate_rows = rejected_rows = 0
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for line, row in enumerate(csv.DictReader(stream), start=2):
            if (not row["external_id"] and not row["contact_external_id"]
                    and not row["email"] and not row["phone"]
                    and row["amount"] == "not-a-number" and not row["gift_date"]):
                rejected_rows += 1
                continue
            identity = row["external_id"]
            if identity in seen:
                if seen[identity] != row:
                    raise ValueError(f"Conflicting duplicate {identity!r} at CSV line {line}")
                duplicate_rows += 1
                continue
            try:
                value = Decimal(row["amount"])
                if not value.is_finite() or value <= 0 or value * 100 != int(value * 100):
                    raise ValueError("amount must be positive, finite, and in whole cents")
                cents = int(value * 100)
                when = _date(row["gift_date"])
                if not identity or not row["contact_external_id"]:
                    raise ValueError("missing gift or contact external ID")
                if row["is_recurring"] not in ("true", "false"):
                    raise ValueError("invalid recurring flag")
            except (ValueError, InvalidOperation) as exc:
                raise ValueError(f"Invalid gift CSV line {line}: {exc}") from exc
            seen[identity] = row
            amounts.append(cents)
            for group, key in ((campaigns, row["campaign"]), (channels, row["channel"])):
                group[key][0] += 1
                group[key][1] += cents
            cohorts[when.year].add(row["contact_external_id"])
            stats = annual[when.year]
            stats["gift_count"] += 1
            stats["cents"] += cents
            stats["monthly_cents"][when.month - 1] += cents
            if when.month >= 11:
                stats["nov_dec_cents"] += cents
            if row["is_recurring"] == "true":
                stats["recurring_monthly_cents"][when.month - 1] += cents
                plans[row["recurring_plan_id"]].append((when, cents))
            else:
                one_time_channels[row["channel"]][0] += 1
                one_time_channels[row["channel"]][1] += cents
                stats["one_time_cents"] += cents
                stats["one_time_monthly_cents"][when.month - 1] += cents
                if when.month >= 11:
                    stats["one_time_nov_dec_cents"] += cents
                if when.month == 12:
                    stats["one_time_dec_cents"] += cents
                    if when.day >= 25:
                        stats["one_time_last_week_dec_cents"] += cents
    if not amounts:
        raise ValueError("No valid gift rows found")
    total = sum(amounts)

    def percent(numerator, denominator):
        return round(100 * numerator / denominator, 6) if denominator else None

    def groups(values):
        return {
            name: {"gifts": count, "dollars": cents / 100,
                   "dollar_share_pct": percent(cents, total),
                   "average_gift": round(cents / count / 100, 2)}
            for name, (count, cents) in sorted(values.items(), key=lambda item: -item[1][1])
        }

    sorted_amounts = sorted(amounts)
    for year, stats in sorted(annual.items()):
        prior = annual.get(year - 1)
        previous = cohorts.get(year - 1, set())
        retained = len(previous & cohorts[year])
        stats.update({
            "dollars": stats["cents"] / 100,
            "one_time_dollars": stats["one_time_cents"] / 100,
            "nov_dec_share_pct": percent(stats["nov_dec_cents"], stats["cents"]),
            "one_time_nov_dec_share_pct": percent(
                stats["one_time_nov_dec_cents"], stats["one_time_cents"]),
            "last_week_share_of_one_time_dec_pct": percent(
                stats["one_time_last_week_dec_cents"], stats["one_time_dec_cents"]),
            "dollar_growth_pct": (percent(stats["cents"] - prior["cents"], prior["cents"])
                                  if prior else None),
            "donors": len(cohorts[year]), "prior_year_donors": len(previous),
            "retained_donors": retained, "retention_pct": percent(retained, len(previous)),
        })
    return {
        "source": str(Path(path).resolve()),
        "basis": "Unique valid CSV gifts; retention by contact_external_id before identity resolution",
        "valid_gifts": len(amounts), "duplicate_rows_excluded": duplicate_rows,
        "invalid_sentinel_rows_excluded": rejected_rows, "total_dollars": total / 100,
        "campaigns": groups(campaigns), "channels": groups(channels),
        "one_time_channels": groups(one_time_channels),
        "gift_sizes": {
            "median": median(amounts) / 100,
            "p90": sorted_amounts[int((len(amounts) - 1) * 0.90)] / 100,
            "p99": sorted_amounts[int((len(amounts) - 1) * 0.99)] / 100,
            "maximum": max(amounts) / 100,
            "under_25": sum(v < 2500 for v in amounts),
            "25_to_under_50": sum(2500 <= v < 5000 for v in amounts),
            "50_to_under_100": sum(5000 <= v < 10000 for v in amounts),
            "100_to_under_500": sum(10000 <= v < 50000 for v in amounts),
            "500_to_under_1000": sum(50000 <= v < 100000 for v in amounts),
            "1000_to_under_10000": sum(100000 <= v < 1000000 for v in amounts),
            "10000_or_more": sum(v >= 1000000 for v in amounts),
            "1000_or_more_pct": percent(sum(v >= 100000 for v in amounts), len(amounts)),
        },
        "annual": dict(sorted(annual.items())),
        "recurring_plans": len(plans),
        "complete_flat_recurring_plans": sum(
            len(rows) == 12 and len({when.month for when, _ in rows}) == 12
            and len({cents for _, cents in rows}) == 1 for rows in plans.values()),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gifts_csv", type=Path)
    args = parser.parse_args()
    print(json.dumps(measure_giving(args.gifts_csv), indent=2, sort_keys=True))
