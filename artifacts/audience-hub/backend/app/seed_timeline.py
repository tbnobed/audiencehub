"""Pure calendar-based acceptance checks; never infer the reference from data."""
from datetime import date

from app.seed_dates import seed_window


def timeline_checks(rows, as_of):
    window = seed_window(as_of)
    full_years = [year for year in range(window.start.year, window.end.year + 1)
                  if window.start <= date(year, 1, 1) and date(year, 12, 31) <= window.end]
    by_key = {(int(row["year"]), bool(row["is_recurring"])): row for row in rows}
    errors, largest = [], {}
    for year in full_years:
        row = by_key.get((year, False), {})
        for dollars_key, year_end_key, label in (
            ("dollars", "gross_year_end", "positive originals"),
            ("retained_dollars", "year_end", "unreturned")):
            dollars = float(row.get(dollars_key) or 0)
            share = 100 * float(row.get(year_end_key) or 0) / dollars if dollars else 0
            if not 25 - 1e-9 <= share <= 35 + 1e-9:
                errors.append(f"{year} Nov-Dec one-time dollars ({label}) {share:.2f}% outside 25–35%")
    for label, recurring_options in (("one-time", (False,)), ("all gifts", (False, True))):
        changes = []
        for previous, current in zip(full_years, full_years[1:]):
            before = sum(int(by_key.get((previous, recurring), {}).get("gifts", 0))
                         for recurring in recurring_options)
            after = sum(int(by_key.get((current, recurring), {}).get("gifts", 0))
                        for recurring in recurring_options)
            change = 100 * (after - before) / before if before else None
            changes.append((previous, current, change))
            if change is None or abs(change) > 25 + 1e-9:
                detail = "undefined (previous year empty)" if change is None else f"{change:+.2f}%"
                errors.append(f"{label} count {previous}→{current}: {detail}; limit ±25%")
        largest[label] = (max(changes, key=lambda item: float("inf") if item[2] is None
                             else abs(item[2])) if changes else None)
    return {"full_years": full_years, "errors": errors, "largest_count_changes": largest}
