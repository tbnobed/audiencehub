"""Synthetic traffic sender. Every event goes through the real HTTP API."""
import hashlib
import os
import time
from urllib.parse import urlsplit
import httpx


def send_events(*, profiles, random_seed, host, write_key=None, contacts_path=None):
    key = write_key or os.environ.get("INGEST_WRITE_KEY")
    parsed = urlsplit(host)
    if not key:
        raise ValueError("--events requires INGEST_WRITE_KEY for an active event API source.")
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("--events-host must be an HTTP(S) API origin without credentials or query.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Non-local event ingestion requires HTTPS.")
    sent = 0
    contacts = []
    if contacts_path:
        import csv
        with open(contacts_path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("email") and row["email"].strip().lower().split("@")[-1] in {
                    "example.com", "example.org", "example.net", "gmail.test"
                }:
                    contacts.append(row)
                if len(contacts) >= profiles:
                    break
    with httpx.Client(base_url=host.rstrip("/") + "/", timeout=30, follow_redirects=False) as client:
        batch = []
        def flush():
            nonlocal sent
            if not batch:
                return
            for attempt in range(4):
                try:
                    response = client.post("v1/batch", headers={"X-Write-Key": key}, json={"batch": batch})
                except httpx.TransportError:
                    if attempt == 3:
                        raise RuntimeError("Event API transport failed; retry with the same random seed.") from None
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code == 429 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code != 202:
                    raise RuntimeError(f"Event API returned HTTP {response.status_code}.")
                result = response.json()
                if result.get("rejected") or result.get("accepted") != len(batch):
                    raise RuntimeError("Event API rejected synthetic messages; review source configuration and suppression policy.")
                sent += len(batch)
                batch.clear()
                return
        for person in range(profiles):
            aid = hashlib.sha256(f"seed:{random_seed}:{person}".encode()).hexdigest()[:32]
            common = {"anonymousId": aid, "context": {"library": "kinship-seed"}}
            contact = contacts[person] if person < len(contacts) else {}
            messages = [
                {"type": "page", "properties": {"path": "/", "title": "Synthetic visitor"}},
                {"type": "track", "event": "Video Watched", "properties": {"video_id": f"video-{person % 20}", "percent": 75}},
                {"type": "identify", "userId": f"seed-{person}",
                 "traits": {"email": contact.get("email", f"seed-{random_seed}-{person}@example.org").strip(),
                            "firstName": contact.get("first_name", "Synthetic"),
                            "lastName": contact.get("last_name", f"Partner {person}")}},
                {"type": "track", "event": "App Opened", "userId": f"seed-{person}", "properties": {"platform": "synthetic-app"}},
            ]
            for index, message in enumerate(messages):
                batch.append({**common, **message, "messageId": f"seed-{random_seed}-{person}-{index}"})
                if len(batch) == 100:
                    flush()
        flush()
    return sent
