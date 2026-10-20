"""Batch-oriented deterministic profile identity resolution."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from app.identity.locking import acquire_identifier_locks, acquire_resolver_lock

from app.identity.normalize import (
    normalize_anonymous_id,
    normalize_external_id,
    normalize_user_id,
)
from app.identity.survivorship import recompute_profiles_fields

BATCH_SIZE = 500
COMPONENT_SCAN_SIZE = 10_000
HIGH_CARDINALITY_LIMIT = 25


class UnionFind:
    def __init__(self):
        self.parent: dict[Any, Any] = {}
        self.rank: dict[Any, int] = {}

    def add(self, item: Any) -> None:
        if item not in self.parent:
            self.parent[item] = item
            self.rank[item] = 0

    def find(self, item: Any) -> Any:
        self.add(item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: Any, right: Any) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def backfill_anonymous_events(db: Session, source_id: int, anonymous_id: str, profile_id: int) -> int:
    """Attach previously anonymous events after an identify operation."""
    result = db.execute(
        text("""
            UPDATE events SET profile_id=:profile_id
            WHERE source_id=:source_id AND anonymous_id=:anonymous_id AND profile_id IS NULL
        """),
        {"profile_id": profile_id, "source_id": source_id, "anonymous_id": anonymous_id},
    )
    return result.rowcount or 0


def _record_identifiers(record: dict, source_key: str) -> set[tuple[str, str]]:
    values: set[tuple[str, str]] = set()
    external = normalize_external_id(source_key, record["external_id"])
    if external:
        values.add(("external", external))
    if record.get("email_norm"):
        values.add(("email", record["email_norm"].strip().lower()))
    if record.get("phone_e164"):
        values.add(("phone", record["phone_e164"]))
    attrs = record.get("attributes") or {}
    if isinstance(attrs, str):
        attrs = json.loads(attrs)
    from app.identity.master import SYSTEM_KEYS
    import hashlib
    for kind in SYSTEM_KEYS:
        if kind == "meta_app_user_id":
            # Meta identifiers enter only from meta_verified_keys in the
            # set-based resolver, never from caller-supplied attributes.
            continue
        if attrs.get(kind):
            value = str(attrs[kind]).strip()
            values.add((kind, value.lower() if kind == "zeta_uid" else value))
    email = str(record.get("email_norm") or "").strip().lower()
    for kind, algorithm in (("email_md5", hashlib.md5), ("email_sha256", hashlib.sha256)):
        value = algorithm(email.encode()).hexdigest() if email else str(attrs.get(kind) or "").strip().lower()
        if value:
            values.add((kind, value))
    reference = attrs.get("_identity_reference") or {}
    if isinstance(reference, dict):
        reference_source = reference.get("source_key")
        reference_external_id = reference.get("contact_external_id")
        if isinstance(reference_source, str) and reference_source.strip():
            scoped_reference = normalize_external_id(reference_source.strip(), reference_external_id)
            if scoped_reference:
                values.add(("external", scoped_reference))
    user_id = normalize_user_id(source_key, attrs.get("user_id"))
    anonymous_id = normalize_anonymous_id(attrs.get("anonymous_id"))
    if user_id:
        values.add(("user_id", user_id))
    if anonymous_id and attrs.get("identify"):
        values.add(("anonymous_id", anonymous_id))
    return values


def _blocked(db: Session, identifiers: set[tuple[str, str]]) -> set[tuple[str, str]]:
    if not identifiers:
        return set()
    pairs = sorted(identifiers)
    rows = db.execute(
        text("""
            SELECT candidate.kind, candidate.value
            FROM unnest(CAST(:types AS text[]), CAST(:values AS text[]))
                 AS candidate(kind, value)
            CROSS JOIN LATERAL (
                SELECT 1 FROM identifier_blocklist b
                WHERE b.type=candidate.kind AND b.value=candidate.value
                LIMIT 1
            ) AS blocked
        """),
        {"types": [kind for kind, _ in pairs], "values": [value for _, value in pairs]},
    ).all()
    blocked = {(kind, value) for kind, value in rows}
    for kind, value in identifiers:
        if kind == "email" and (
            value == "test@test.com" or value == "none@none.com" or value == "no@email.com"
            or value == "na@na.com" or value.startswith("noemail@") or value.endswith("@test.com")
        ):
            blocked.add((kind, value))
        if kind == "phone":
            digits = "".join(character for character in value if character.isdigit())
            if digits.startswith("1") and len(digits) == 11:
                digits = digits[1:]
            if len(digits) == 10 and len(set(digits)) == 1:
                blocked.add((kind, value))
    return blocked


def _auto_block_high_cardinality(db: Session, identifiers: set[tuple[str, str]]) -> set[tuple[str, str]]:
    candidate_values = {
        kind: sorted(value for candidate_kind, value in identifiers if candidate_kind == kind)
        for kind in ("email", "phone")
    }
    blocked: set[tuple[str, str]] = set()
    for kind, column in (("email", "email_norm"), ("phone", "phone_e164")):
        values = candidate_values[kind]
        if not values:
            continue
        candidate_value = (
            "CAST(candidate.value AS citext)" if kind == "email" else "candidate.value"
        )
        # Keep each equality predicate on the indexed source-record column.
        # Splitting these avoids the OR join, which made PostgreSQL scan the
        # entire source_records table for every batch.
        rows = db.execute(
            text(f"""
                SELECT sr.source_id, candidate.value,
                       count(DISTINCT sr.external_id) AS records
                FROM unnest(CAST(:values AS text[])) AS candidate(value)
                JOIN source_records sr ON sr.{column}={candidate_value}
                LEFT JOIN imports i ON i.id=sr.last_import_id
                WHERE i.record_type IS DISTINCT FROM 'gift'
                  AND i.record_type IS DISTINCT FROM 'event'
                GROUP BY sr.source_id, candidate.value
                HAVING count(DISTINCT sr.external_id) > :threshold
            """),
            {"values": values, "threshold": HIGH_CARDINALITY_LIMIT},
        ).all()
        blocked.update((kind, value) for _, value, _ in rows)
    for kind, value in sorted(blocked):
        db.execute(
            text("""
                INSERT INTO identifier_blocklist (type, value, reason)
                VALUES (:type, :value, 'high_cardinality')
                ON CONFLICT (type, value) DO NOTHING
            """),
            {"type": kind, "value": value},
        )
    return blocked


def _move_profile_references(db: Session, winner_id: int, loser_id: int) -> None:
    db.execute(text("UPDATE identifiers SET profile_id=:winner WHERE profile_id=:loser"),
               {"winner": winner_id, "loser": loser_id})
    db.execute(text("UPDATE source_records SET profile_id=:winner WHERE profile_id=:loser"),
               {"winner": winner_id, "loser": loser_id})
    db.execute(text("UPDATE gifts SET profile_id=:winner WHERE profile_id=:loser"),
               {"winner": winner_id, "loser": loser_id})
    db.execute(text("UPDATE events SET profile_id=:winner WHERE profile_id=:loser"),
               {"winner": winner_id, "loser": loser_id})

    consents = db.execute(
        text("SELECT * FROM consents WHERE profile_id=:loser ORDER BY channel"),
        {"loser": loser_id},
    ).mappings().all()
    for consent in consents:
        old = db.execute(
            text("SELECT * FROM consents WHERE profile_id=:winner AND channel=:channel"),
            {"winner": winner_id, "channel": consent["channel"]},
        ).mappings().first()
        prefer_loser = old is None or db.scalar(text(
            "SELECT consent_candidate_wins(:os,:ot,:oi,:ns,:nt,:ni)"
        ), {"os": old["status"], "ot": old["captured_at"], "oi": old["source_id"],
            "ns": consent["status"], "nt": consent["captured_at"], "ni": consent["source_id"]})
        if prefer_loser:
            db.execute(
                text("""
                    INSERT INTO consents (profile_id, channel, status, source_id, captured_at, evidence)
                    VALUES (:profile_id, :channel, :status, :source_id, :captured_at,
                            CAST(:evidence AS jsonb))
                    ON CONFLICT (profile_id, channel) DO UPDATE SET status=EXCLUDED.status,
                      source_id=EXCLUDED.source_id, captured_at=EXCLUDED.captured_at,
                      evidence=EXCLUDED.evidence
                """),
                {
                    **dict(consent),
                    "profile_id": winner_id,
                    "evidence": json.dumps(consent["evidence"]),
                },
            )
    db.execute(text("DELETE FROM consents WHERE profile_id=:loser"), {"loser": loser_id})

    db.execute(
        text("""
            INSERT INTO enrichment_values
              (profile_id, source_id, attribute_key, value_text, value_num, value_bool,
               value_date, imported_at, license_expires_at, vendor, license_id, append_date, sensitive)
            SELECT :winner, source_id, attribute_key, value_text, value_num, value_bool,
                   value_date, imported_at, license_expires_at, vendor, license_id, append_date, sensitive
            FROM enrichment_values WHERE profile_id=:loser
            ORDER BY source_id, attribute_key
            ON CONFLICT (profile_id, source_id, attribute_key) DO UPDATE SET
              value_text=EXCLUDED.value_text, value_num=EXCLUDED.value_num,
              value_bool=EXCLUDED.value_bool, value_date=EXCLUDED.value_date,
              imported_at=EXCLUDED.imported_at, license_expires_at=EXCLUDED.license_expires_at,
              vendor=EXCLUDED.vendor, license_id=EXCLUDED.license_id,
              append_date=EXCLUDED.append_date, sensitive=EXCLUDED.sensitive
            WHERE enrichment_values.imported_at < EXCLUDED.imported_at
        """),
        {"winner": winner_id, "loser": loser_id},
    )
    db.execute(text("DELETE FROM enrichment_values WHERE profile_id=:loser"), {"loser": loser_id})
    db.execute(text("DELETE FROM segment_membership WHERE profile_id=:loser"), {"loser": loser_id})
    db.execute(text("DELETE FROM profile_traits WHERE profile_id=:loser"), {"loser": loser_id})


def _materialize_source_attributes_batch(db: Session, assignments: list[tuple[dict, int]]) -> None:
    consents: list[dict] = []
    enrichments: list[dict] = []
    for record, profile_id in assignments:
        attrs = record.get("attributes") or {}
        if isinstance(attrs, str):
            attrs = json.loads(attrs)
        consent = attrs.get("_import_consent")
        if consent and consent.get("channel") and consent.get("status"):
            consents.append({
                "profile_id": profile_id, "channel": consent["channel"],
                "status": consent["status"], "source_id": record["source_id"],
                "captured_at": consent.get("captured_at"),
                "source_record_id": record["id"],
            })
        for key, value in (attrs.get("_import_enrichments") or {}).items():
            data_type = value.get("data_type")
            if data_type not in {"text", "enum", "number", "boolean", "date"}:
                continue
            enrichments.append({
                "profile_id": profile_id, "source_id": record["source_id"], "key": key,
                "data_type": data_type, "value": value.get("value"),
                "imported_at": record["updated_at"],
                "vendor": value.get("vendor"), "license_id": value.get("license_id"),
                "append_date": value.get("append_date"), "sensitive": bool(value.get("sensitive")),
            })
    if consents:
        consents.sort(key=lambda row: (row["profile_id"], row["channel"]))
        db.execute(
            text("""
                INSERT INTO consents (profile_id, channel, status, source_id, captured_at, evidence)
                VALUES (:profile_id, :channel, :status, :source_id,
                        COALESCE(CAST(:captured_at AS timestamptz), now()),
                        jsonb_build_object('source_record_id', :source_record_id))
                ON CONFLICT (profile_id, channel) DO UPDATE SET status=EXCLUDED.status,
                  source_id=EXCLUDED.source_id, captured_at=EXCLUDED.captured_at,
                  evidence=EXCLUDED.evidence
                WHERE consent_candidate_wins(consents.status,consents.captured_at,consents.source_id,
                  EXCLUDED.status,EXCLUDED.captured_at,EXCLUDED.source_id)
            """),
            consents,
        )
    if enrichments:
        enrichments.sort(key=lambda row: (row["profile_id"], row["source_id"], row["key"]))
        db.execute(
            text("""
                INSERT INTO enrichment_values
                  (profile_id, source_id, attribute_key, value_text, value_num, value_bool,
                   value_date, imported_at, vendor, license_id, append_date, sensitive)
                VALUES (:profile_id, :source_id, :key,
                    CASE WHEN :data_type IN ('text','enum') THEN CAST(:value AS text) END,
                    CASE WHEN :data_type='number' THEN CAST(:value AS numeric) END,
                    CASE WHEN :data_type='boolean' THEN CAST(:value AS boolean) END,
                    CASE WHEN :data_type='date' THEN CAST(:value AS date) END,
                    :imported_at, :vendor, :license_id, CAST(:append_date AS date), :sensitive)
                ON CONFLICT (profile_id, source_id, attribute_key) DO UPDATE SET
                  value_text=EXCLUDED.value_text, value_num=EXCLUDED.value_num,
                  value_bool=EXCLUDED.value_bool, value_date=EXCLUDED.value_date,
                  imported_at=EXCLUDED.imported_at, vendor=EXCLUDED.vendor,
                  license_id=EXCLUDED.license_id, append_date=EXCLUDED.append_date,
                  sensitive=EXCLUDED.sensitive
                WHERE enrichment_values.imported_at < EXCLUDED.imported_at
            """),
            enrichments,
        )


def _assign_record_references(
    db: Session, records_by_id: dict[int, dict], record_profiles: dict[int, int]
) -> None:
    if not record_profiles:
        return
    record_ids = sorted(record_profiles)
    profile_ids = [record_profiles[record_id] for record_id in record_ids]
    db.execute(
        text("""
            UPDATE source_records sr
            SET profile_id=assignment.profile_id, resolved_at=now()
            FROM unnest(CAST(:record_ids AS bigint[]), CAST(:profile_ids AS bigint[]))
                 AS assignment(record_id, profile_id)
            WHERE sr.id=assignment.record_id
        """),
        {"record_ids": record_ids, "profile_ids": profile_ids},
    )
    _materialize_source_attributes_batch(
        db, [(records_by_id[record_id], profile_id)
             for record_id, profile_id in record_profiles.items()]
    )


def resolve_batch(db: Session, limit: int = BATCH_SIZE, job_id: int | None = None) -> dict[str, int]:
    """Resolve at most 500 records; caller commits and loops until zero.

    Components crossing the batch boundary are rebuilt under the exclusive
    identity lock on every call. Later bridges journal and merge the previously
    assigned profiles, preserving the oldest issued master.
    """
    # Both paths use the same set-based conflict-safe graph and journal.
    from app.identity.bulk import resolve_bulk
    return resolve_bulk(db, job_id=job_id, mode="incremental", limit=limit)