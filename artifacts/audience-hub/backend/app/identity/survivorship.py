"""Deterministic source-priority selection for profile fields."""

from sqlalchemy import text
from sqlalchemy.orm import Session


def select_survivorship_values(records: list[dict]) -> dict:
    """Choose source record values, with address fields treated as one block."""
    ordered = sorted(
        records,
        key=lambda row: (row["priority"], -(row["updated_at"].timestamp()), row["id"]),
    )

    def first_nonempty(key: str):
        for record in ordered:
            value = record.get(key)
            if value is not None and (not isinstance(value, str) or value.strip()):
                return value
        return None

    name = next(
        (row for row in ordered if row.get("first_name") or row.get("last_name")),
        None,
    )
    address = next(
        (
            row
            for row in ordered
            if any(row.get(key) for key in ("address1", "city", "region", "postal_code", "country"))
        ),
        None,
    )
    return {
        "email": first_nonempty("email_norm"),
        "phone": first_nonempty("phone_e164"),
        "first_name": name.get("first_name") if name else None,
        "last_name": name.get("last_name") if name else None,
        **{
            key: (address.get(key) if address else None)
            for key in ("address1", "city", "region", "postal_code", "country")
        },
    }


def recompute_profile_fields(db: Session, profile_id: int) -> None:
    recompute_profiles_fields(db, [profile_id])


def recompute_profiles_fields(db: Session, profile_ids: list[int] | set[int]) -> None:
    """Recompute fields for a set of profiles with one read and one write."""
    if not profile_ids:
        return
    records = db.execute(
        text("""
            SELECT sr.profile_id, sr.id, sr.email_norm, sr.phone_e164, sr.first_name, sr.last_name,
                   sr.address1, sr.city, sr.region, sr.postal_code, sr.country,
                   sr.updated_at, s.priority
            FROM source_records sr
            JOIN sources s ON s.id=sr.source_id
            WHERE sr.profile_id=ANY(:profile_ids)
        """),
        {"profile_ids": list(profile_ids)},
    ).mappings().all()
    grouped: dict[int, list[dict]] = {}
    for row in records:
        grouped.setdefault(row["profile_id"], []).append(dict(row))
    params = []
    for profile_id in profile_ids:
        values = select_survivorship_values(grouped.get(profile_id, []))
        params.append({
            **values,
            **{f"search_{key}": value for key, value in values.items()},
            "profile_id": profile_id,
        })
    if not params:
        return
    fields = (
        "email", "phone", "first_name", "last_name", "address1",
        "city", "region", "postal_code", "country",
    )
    arrays = {
        "profile_ids": [row["profile_id"] for row in params],
        **{field: [row[field] for row in params] for field in fields},
    }
    db.execute(
        text("""
            UPDATE profiles p SET email=v.email, phone=v.phone, first_name=v.first_name,
                last_name=v.last_name, address1=v.address1, city=v.city, region=v.region,
                postal_code=v.postal_code, country=v.country,
                search_text=trim(concat_ws(' ', v.email, v.phone, v.first_name, v.last_name,
                    v.address1, v.city, v.region, v.postal_code, v.country))
            FROM unnest(CAST(:profile_ids AS bigint[]), CAST(:email AS text[]),
                CAST(:phone AS text[]), CAST(:first_name AS text[]), CAST(:last_name AS text[]),
                CAST(:address1 AS text[]), CAST(:city AS text[]), CAST(:region AS text[]),
                CAST(:postal_code AS text[]), CAST(:country AS text[]))
                AS v(profile_id, email, phone, first_name, last_name, address1, city, region,
                     postal_code, country)
            WHERE p.id=v.profile_id
        """),
        arrays,
    )