"""Compatibility entry point for the former --overview benchmark switch."""
from app.dashboards.scale_benchmark import measure as measure_dashboards


def measure(directory, gift_rows, source_rows):
    # New evidence measures the complete ASGI dashboard surface, not the removed
    # live consent/HMAC algorithm. --dashboards --profiles controls size directly.
    return measure_dashboards(directory, min(500_000, gift_rows))