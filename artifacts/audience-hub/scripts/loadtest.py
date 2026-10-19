#!/usr/bin/env python3
"""Explicit server-only HTTP workload. Writes isolated benchmark traffic, not a seed.

Uses only the standard library; credentials come from operator environment.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import http.cookiejar
import json
import math
import os
from pathlib import Path
import statistics
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError, URLError
from uuid import uuid4
from datetime import datetime, timezone


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, math.ceil(len(ordered)*fraction)-1)] if ordered else None


def request(opener, url, body=None, headers=None):
    start = time.monotonic()
    data = None if body is None else json.dumps(body).encode()
    try:
        with opener.open(Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})}), timeout=120) as response:
            value = json.load(response)
            return response.status, value, (time.monotonic()-start)*1000
    except HTTPError as exc:
        return exc.code, {}, (time.monotonic()-start)*1000
    except (OSError, URLError, ValueError):
        return 0, {}, (time.monotonic()-start)*1000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--segment-id", type=int, required=True)
    parser.add_argument("--output-dir", default="/data/benchmarks")
    parser.add_argument("--allow-http-localhost", action="store_true")
    parser.add_argument("--acknowledge-writes", action="store_true", required=True)
    args = parser.parse_args()
    parsed = urlparse(args.base_url)
    if parsed.scheme != "https" and not (args.allow_http_localhost and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        parser.error("Use HTTPS, or explicitly allow HTTP localhost")
    if not 1 <= args.requests <= 100000 or not 1 <= args.batch_size <= 100 or not 1 <= args.concurrency <= 64:
        parser.error("Requests 1..100000, batch size 1..100, concurrency 1..64")
    required = ["KINSHIP_LOADTEST_WRITE_KEY", "KINSHIP_LOADTEST_EMAIL", "KINSHIP_LOADTEST_PASSWORD"]
    if any(not os.environ.get(key) for key in required):
        parser.error("Set " + ", ".join(required) + " in the server environment; never pass secrets as CLI arguments")
    base = args.base_url.rstrip("/")
    session = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    code, bootstrap, _ = request(session, base+"/auth/local/session")
    code, _, _ = request(session, base+"/auth/local/login",
        {"email": os.environ[required[1]], "password": os.environ[required[2]], "totp": os.environ.get("KINSHIP_LOADTEST_TOTP", "")},
        {"X-CSRF-Token": bootstrap.get("csrf_token", "")})
    if code != 200:
        raise SystemExit("Local sign-in failed (check credentials, lockout or MFA); no workload sent")
    code, me, _ = request(session, base+"/api/me")
    code, segment, _ = request(session, base+f"/api/segments/{args.segment_id}")
    if code != 200 or not segment.get("definition"):
        raise SystemExit("Segment unavailable; no workload sent")
    run_id = str(uuid4())
    worker_local = threading.local()

    def run_one(index):
        if not hasattr(worker_local, "opener"):
            worker_local.opener = build_opener()
        messages = [{"type": "track", "event": "Kinship Load Test",
            "anonymousId": f"benchmark:{run_id}:{index}:{item}", "messageId": f"{run_id}:{index}:{item}",
            "properties": {"benchmark_run": run_id}} for item in range(args.batch_size)]
        # Validation accepts name/event via the track endpoint contract.
        ingest_code, result, ingest_ms = request(worker_local.opener, base+"/v1/batch",
            {"batch": messages}, {"X-Write-Key": os.environ[required[0]]})
        if result.get("rejected") or result.get("accepted", 0) + result.get("duplicates", 0) != len(messages):
            ingest_code = 422
        return ingest_code, ingest_ms

    started = time.monotonic()
    previews = []
    def preview_requests():
        for _ in range(min(args.requests, 100)):
            code, value, latency = request(session, base+"/api/segments/preview",
                {"definition": segment["definition"]}, {"X-CSRF-Token": me["csrf_token"]})
            previews.append((code, latency))
    with ThreadPoolExecutor(max_workers=1) as preview_pool:
        preview_future = preview_pool.submit(preview_requests)
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            ingestion = list(pool.map(run_one, range(args.requests)))
        seconds = time.monotonic()-started
        preview_future.result()
    # Include errors in the report; never describe offered messages as persisted.
    good = [latency for code, latency in ingestion if 200 <= code < 300]
    preview_good = [latency for code, latency in previews if code == 200]
    result = {
        "run_id": run_id, "at": datetime.now(timezone.utc).isoformat(),
        "requests": args.requests, "batch_size": args.batch_size, "concurrency": args.concurrency,
        "ingestion": {"wall_seconds": seconds, "successful_requests": len(good),
                      "failed_requests": len(ingestion)-len(good),
                      "accepted_messages_per_second": len(good)*args.batch_size/seconds,
                      "p50_ms": percentile(good, .5), "p95_ms": percentile(good, .95),
                      "http_statuses": {str(code): sum(c == code for c, _ in ingestion) for code, _ in ingestion}},
        "segment_preview": {"segment_id": args.segment_id, "successful_requests": len(preview_good),
                            "failed_requests": len(previews)-len(preview_good),
                            "first_ms": previews[0][1], "p50_ms": percentile(preview_good, .5),
                            "p95_ms": percentile(preview_good, .95)},
        "note": "HTTP acceptance throughput, not resolved-event throughput; first request is not a cold-cache guarantee",
    }
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"loadtest-{run_id}.json"
    path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"Results: {path}")
    return 0 if len(good) == len(ingestion) and len(preview_good) == len(previews) else 1


if __name__ == "__main__":
    raise SystemExit(main())
