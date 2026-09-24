"""Batch-oriented deterministic profile identity resolution."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.identity.normalize import (
    normalize_anonymous_id,
    normalize_external_id,
    normalize_user_id,
)
from app.identity.survivorship import recompute_profiles_fields

BATCH_SIZE = 10_000
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
        text("SELECT * FROM consents WHERE profile_id=:loser"),
        {"loser": loser_id},
    ).mappings().all()
    for consent in consents:
        old = db.execute(
            text("SELECT * FROM consents WHERE profile_id=:winner AND channel=:channel"),
            {"winner": winner_id, "channel": consent["channel"]},
        ).mappings().first()
        prefer_loser = old is None or (
            consent["status"] == "opted_out" and old["status"] != "opted_out"
        ) or (
            (consent["status"] == "opted_out") == (old["status"] == "opted_out")
            and consent["captured_at"] > old["captured_at"]
        )
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
               value_date, imported_at, license_expires_at)
            SELECT :winner, source_id, attribute_key, value_text, value_num, value_bool,
                   value_date, imported_at, license_expires_at
            FROM enrichment_values WHERE profile_id=:loser
            ON CONFLICT (profile_id, source_id, attribute_key) DO UPDATE SET
              value_text=EXCLUDED.value_text, value_num=EXCLUDED.value_num,
              value_bool=EXCLUDED.value_bool, value_date=EXCLUDED.value_date,
              imported_at=EXCLUDED.imported_at, license_expires_at=EXCLUDED.license_expires_at
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
            })
    if consents:
        db.execute(
            text("""
                INSERT INTO consents (profile_id, channel, status, source_id, captured_at, evidence)
                VALUES (:profile_id, :channel, :status, :source_id,
                        COALESCE(CAST(:captured_at AS timestamptz), now()),
                        jsonb_build_object('source_record_id', :source_record_id))
                ON CONFLICT (profile_id, channel) DO UPDATE SET status=EXCLUDED.status,
                  source_id=EXCLUDED.source_id, captured_at=EXCLUDED.captured_at,
                  evidence=EXCLUDED.evidence
                WHERE (EXCLUDED.status='opted_out' AND consents.status <> 'opted_out')
                   OR (EXCLUDED.status=consents.status
                       AND consents.captured_at < EXCLUDED.captured_at)
            """),
            consents,
        )
    if enrichments:
        db.execute(
            text("""
                INSERT INTO enrichment_values
                  (profile_id, source_id, attribute_key, value_text, value_num, value_bool,
                   value_date, imported_at)
                VALUES (:profile_id, :source_id, :key,
                    CASE WHEN :data_type IN ('text','enum') THEN CAST(:value AS text) END,
                    CASE WHEN :data_type='number' THEN CAST(:value AS numeric) END,
                    CASE WHEN :data_type='boolean' THEN CAST(:value AS boolean) END,
                    CASE WHEN :data_type='date' THEN CAST(:value AS date) END,
                    :imported_at)
                ON CONFLICT (profile_id, source_id, attribute_key) DO UPDATE SET
                  value_text=EXCLUDED.value_text, value_num=EXCLUDED.value_num,
                  value_bool=EXCLUDED.value_bool, value_date=EXCLUDED.value_date,
                  imported_at=EXCLUDED.imported_at
                WHERE enrichment_values.imported_at < EXCLUDED.imported_at
            """),
            enrichments,
        )


def _assign_record_references(
    db: Session, records_by_id: dict[int, dict], record_profiles: dict[int, int]
) -> None:
    if not record_profiles:
        return
    record_ids = list(record_profiles)
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
    gift_record_ids = [
        record_id for record_id in record_ids
        if records_by_id[record_id].get("imported_record_type") == "gift"
    ]
    if gift_record_ids:
        # Drive this update from gift keys so PostgreSQL uses the existing
        # (source_id, external_id) unique index instead of scanning gifts by
        # source_record_id, which has no dedicated index.
        db.execute(
            text("""
                UPDATE gifts g SET profile_id=gift_link.profile_id
                FROM unnest(CAST(:source_ids AS bigint[]), CAST(:external_ids AS text[]),
                            CAST(:profile_ids AS bigint[]))
                     AS gift_link(source_id, external_id, profile_id)
                CROSS JOIN LATERAL (
                    SELECT candidate.ctid FROM gifts candidate
                    WHERE candidate.source_id=gift_link.source_id
                      AND candidate.external_id=gift_link.external_id
                      AND candidate.profile_id IS NULL
                    LIMIT 1
                ) AS matching_gift
                WHERE g.ctid=matching_gift.ctid
            """),
            {
                "source_ids": [records_by_id[rid]["source_id"] for rid in gift_record_ids],
                "external_ids": [records_by_id[rid]["external_id"] for rid in gift_record_ids],
                "profile_ids": [record_profiles[rid] for rid in gift_record_ids],
            },
        )
    event_links = []
    for record_id, profile_id in record_profiles.items():
        record = records_by_id[record_id]
        event_links.extend((
            (record["source_id"], record["raw_hash"], record_id, profile_id),
            (record["source_id"], record["external_id"], record_id, profile_id),
        ))
    db.execute(
        text("""
            UPDATE events e SET profile_id=event_link.profile_id
            FROM (
                SELECT DISTINCT ON (source_id, message_id)
                       source_id, message_id, profile_id
                FROM unnest(CAST(:source_ids AS bigint[]), CAST(:message_ids AS text[]),
                            CAST(:record_ids AS bigint[]), CAST(:profile_ids AS bigint[]))
                     AS candidate(source_id, message_id, record_id, profile_id)
                ORDER BY source_id, message_id, record_id
            ) AS event_link
            CROSS JOIN LATERAL (
                SELECT candidate_event.tableoid, candidate_event.ctid
                FROM events candidate_event
                WHERE candidate_event.source_id=event_link.source_id
                  AND candidate_event.message_id=event_link.message_id
                  AND candidate_event.profile_id IS NULL
            ) AS matching_event
            WHERE e.tableoid=matching_event.tableoid AND e.ctid=matching_event.ctid
        """),
        {
            "source_ids": [item[0] for item in event_links],
            "message_ids": [item[1] for item in event_links],
            "record_ids": [item[2] for item in event_links],
            "profile_ids": [item[3] for item in event_links],
        },
    )
    _materialize_source_attributes_batch(
        db, [(records_by_id[record_id], profile_id)
             for record_id, profile_id in record_profiles.items()]
    )
    anonymous_links = []
    for record_id, profile_id in record_profiles.items():
        record = records_by_id[record_id]
        attrs = record.get("attributes") or {}
        if isinstance(attrs, str):
            attrs = json.loads(attrs)
        anonymous_id = normalize_anonymous_id(attrs.get("anonymous_id"))
        if anonymous_id and attrs.get("identify"):
            anonymous_links.append((record["source_id"], anonymous_id, profile_id))
    if anonymous_links:
        db.execute(
            text("""
                UPDATE events e SET profile_id=anonymous_link.profile_id
                FROM (
                    SELECT DISTINCT ON (source_id, anonymous_id)
                           source_id, anonymous_id, profile_id
                    FROM unnest(CAST(:source_ids AS bigint[]), CAST(:anonymous_ids AS text[]),
                                CAST(:profile_ids AS bigint[]))
                         AS links(source_id, anonymous_id, profile_id)
                    ORDER BY source_id, anonymous_id, profile_id
                ) AS anonymous_link
                WHERE e.source_id=anonymous_link.source_id
                  AND e.anonymous_id=anonymous_link.anonymous_id AND e.profile_id IS NULL
            """),
            {
                "source_ids": [item[0] for item in anonymous_links],
                "anonymous_ids": [item[1] for item in anonymous_links],
                "profile_ids": [item[2] for item in anonymous_links],
            },
        )


def resolve_batch(db: Session, limit: int = BATCH_SIZE, job_id: int | None = None) -> dict[str, int]:
    """Resolve up to ``limit`` pending source records in one transaction."""
    limit = max(1, min(int(limit), BATCH_SIZE))
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('identity.resolve_batch'))"))
    pending = db.execute(
        text("""
            SELECT sr.*, s.key AS source_key, s.priority, i.record_type AS imported_record_type
            FROM source_records sr JOIN sources s ON s.id=sr.source_id
            LEFT JOIN imports i ON i.id=sr.last_import_id
            WHERE sr.resolved_at IS NULL ORDER BY sr.id LIMIT :limit FOR UPDATE OF sr
        """),
        {"limit": limit},
    ).mappings().all()
    if not pending:
        return {"records": 0, "profiles_created": 0, "merges": 0}

    records = [dict(row) for row in pending]
    identifiers = {record["id"]: _record_identifiers(record, record["source_key"]) for record in records}
    key_set = {identifier for values in identifiers.values() for identifier in values}
    blocked = _blocked(db, key_set)
    blocked |= _auto_block_high_cardinality(db, key_set)
    key_set -= blocked
    for record_id, values in identifiers.items():
        identifiers[record_id] = {identifier for identifier in values if identifier not in blocked}

    lock_keys = [f"{kind}:{value}" for kind, value in sorted(key_set)]
    if lock_keys:
        db.execute(
            text("""
                WITH ordered_keys AS MATERIALIZED (
                    SELECT lock_key FROM unnest(CAST(:lock_keys AS text[])) AS keys(lock_key)
                    ORDER BY lock_key
                )
                SELECT pg_advisory_xact_lock(hashtext(lock_key)) FROM ordered_keys
            """),
            {"lock_keys": lock_keys},
        )
    # Re-read after taking locks so a prior resolver's writes are visible before
    # any new profile is selected.
    key_pairs = sorted(key_set)
    existing = db.execute(
        text("""
            SELECT candidate.kind, candidate.value, matched.profile_id
            FROM unnest(CAST(:types AS text[]), CAST(:values AS text[]))
                 AS candidate(kind, value)
            CROSS JOIN LATERAL (
                SELECT i.profile_id FROM identifiers i
                WHERE i.type=candidate.kind AND i.value=candidate.value
                LIMIT 1
            ) AS matched
        """),
        {"types": [kind for kind, _ in key_pairs], "values": [value for _, value in key_pairs]},
    ).all()
    key_profiles: dict[tuple[str, str], int] = {(kind, value): profile_id
                                                for kind, value, profile_id in existing}
    uf = UnionFind()
    for record in records:
        uf.add(("record", record["id"]))
        if record.get("profile_id"):
            uf.union(("record", record["id"]), ("profile", record["profile_id"]))
        for kind, value in identifiers[record["id"]]:
            if (kind, value) in key_profiles:
                uf.union(("record", record["id"]), ("profile", key_profiles[(kind, value)]))
    key_records: dict[tuple[str, str], list[int]] = defaultdict(list)
    for record_id, values in identifiers.items():
        for kind, value in values:
            key_records[(kind, value)].append(record_id)
    for related in key_records.values():
        for record_id in related[1:]:
            uf.union(("record", related[0]), ("record", record_id))

    components: dict[Any, list[int]] = defaultdict(list)
    for record in records:
        components[uf.find(("record", record["id"]))].append(record["id"])
    record_by_id = {record["id"]: record for record in records}

    component_plans = []
    existing_profile_ids = {profile_id for profile_id in key_profiles.values()}
    existing_profile_ids.update(
        record["profile_id"] for record in records if record.get("profile_id")
    )
    for component_records in components.values():
        root = uf.find(("record", component_records[0]))
        profile_ids = {
            item[1] for item in uf.parent
            if item[0] == "profile" and uf.find(item) == root
        }
        component_plans.append((component_records, profile_ids))

    profile_first_seen = {}
    if existing_profile_ids:
        profile_rows = db.execute(
            text("SELECT id, first_seen_at FROM profiles WHERE id=ANY(:ids)"),
            {"ids": list(existing_profile_ids)},
        ).all()
        profile_first_seen = {row.id: row.first_seen_at for row in profile_rows}
    new_profile_components = [
        component_records for component_records, profile_ids in component_plans if not profile_ids
    ]
    created_profile_ids = []
    if new_profile_components:
        created_profile_ids = db.execute(
            text("""
                INSERT INTO profiles (first_seen_at, last_seen_at)
                SELECT now(), now() FROM generate_series(1, :count)
                RETURNING id
            """),
            {"count": len(new_profile_components)},
        ).scalars().all()

    component_profile_ids = {}
    new_profile_iterator = iter(created_profile_ids)
    for component_records, profile_ids in component_plans:
        if not profile_ids:
            component_profile_ids[tuple(component_records)] = next(new_profile_iterator)
        else:
            component_profile_ids[tuple(component_records)] = min(
                profile_ids, key=lambda profile_id: (profile_first_seen[profile_id], profile_id)
            )

    profiles_created = len(new_profile_components)
    merges = 0
    affected: set[int] = set()
    record_profiles: dict[int, int] = {}
    key_profiles_to_write: dict[tuple[str, str], int] = {}
    for component_records, profile_ids in component_plans:
        profile_id = component_profile_ids[tuple(component_records)]
        affected.add(profile_id)
        record_profiles.update({record_id: profile_id for record_id in component_records})
        for record_id in component_records:
            for kind, value in identifiers[record_id]:
                key_profiles_to_write[(kind, value)] = profile_id
        for loser in sorted(profile_ids - {profile_id}):
            link = next(
                (
                    {"type": kind, "value": value}
                    for rid in component_records
                    for kind, value in identifiers[rid]
                    if key_profiles.get((kind, value)) == loser
                ),
                {
                    "type": "external",
                    "value": next(
                        (
                            f"{record_by_id[rid]['source_key']}:{record_by_id[rid]['external_id']}"
                            for rid in component_records
                            if record_by_id[rid].get("profile_id") == loser
                        ),
                        record_by_id[component_records[0]]["external_id"],
                    ),
                },
            )
            _move_profile_references(db, profile_id, loser)
            db.execute(
                text("UPDATE profiles SET merged_into_id=:winner WHERE id=:loser"),
                {"winner": profile_id, "loser": loser},
            )
            db.execute(
                text("""
                    INSERT INTO profile_merges (winner_id, loser_id, reason, job_id)
                    VALUES (:winner, :loser, CAST(:reason AS jsonb), :job_id)
                """),
                {"winner": profile_id, "loser": loser,
                 "reason": json.dumps(link), "job_id": job_id},
            )
            merges += 1

    _assign_record_references(db, record_by_id, record_profiles)
    if key_profiles_to_write:
        key_rows = list(key_profiles_to_write.items())
        db.execute(
            text("""
                INSERT INTO identifiers (type, value, profile_id)
                SELECT key_type, key_value, profile_id
                FROM unnest(CAST(:types AS text[]), CAST(:values AS text[]),
                            CAST(:profile_ids AS bigint[]))
                     AS keys(key_type, key_value, profile_id)
                ON CONFLICT (type, value) DO UPDATE SET profile_id=EXCLUDED.profile_id
                WHERE identifiers.profile_id IS DISTINCT FROM EXCLUDED.profile_id
            """),
            {
                "types": [item[0][0] for item in key_rows],
                "values": [item[0][1] for item in key_rows],
                "profile_ids": [item[1] for item in key_rows],
            },
        )
    recompute_profiles_fields(db, affected)
    if affected:
        db.execute(
            text("DELETE FROM profile_traits WHERE profile_id=ANY(CAST(:profile_ids AS bigint[]))"),
            {"profile_ids": list(affected)},
        )
        # Re-resolution may change gift/event ownership or merge profiles. Reset
        # the debounce timestamp on conflict so traits run ten minutes after the
        # final resolution activity for each profile.
        db.execute(text("""
            INSERT INTO trait_dirty_profiles (profile_id, dirtied_at)
            SELECT DISTINCT profile_id, clock_timestamp()
            FROM unnest(CAST(:profile_ids AS bigint[])) AS changed(profile_id)
            ON CONFLICT (profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at
        """), {"profile_ids": list(affected)})
    return {"records": len(records), "profiles_created": profiles_created, "merges": merges}