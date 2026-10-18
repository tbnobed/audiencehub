"""Export whitelist and batch-sized, license-aware mapping resolution."""
from datetime import date
from sqlalchemy import text
from app.models import ProfileTrait
from app.canonical import FIELDS

PROFILE_FIELDS = {"id", "master_id", "email", "phone", "first_name", "last_name", "address1",
                  "city", "region", "postal_code", "country", "first_seen_at", "last_seen_at"}
PII_FIELDS = {"email", "phone", "first_name", "last_name", "address1", "postal_code"}
TRAIT_FIELDS = set(ProfileTrait.__table__.columns.keys()) - {"profile_id"}


def registry(db, role="admin"):
    fields = [{"key": key, "label": key.replace("_", " ").capitalize().replace("Donor", "Partner"),
               "kind": "profile", "pii": key in PII_FIELDS} for key in sorted(PROFILE_FIELDS)]
    fields += [{"key": key, "label": key.replace("_", " ").capitalize().replace("Donor", "Partner"),
                "kind": "trait", "pii": False} for key in sorted(TRAIT_FIELDS)]
    fields += [{"key": key, "label": key.replace("_", " ").capitalize(),
                "kind": "canonical", "pii": key in PII_FIELDS or key in {"address2", "donor_account_number"},
                "sensitive": bool(meta.get("sensitive"))}
               for key, meta in sorted(FIELDS.items())]
    for row in db.execute(text("""SELECT s.key,a.key AS attribute_key,
        COALESCE((to_jsonb(a)->>'sensitive')::boolean,false) OR EXISTS(
          SELECT 1 FROM enrichment_values e WHERE e.source_id=a.source_id
          AND e.attribute_key=a.key AND e.sensitive) AS sensitive
        FROM enrichment_attributes a
        JOIN sources s ON s.id=a.source_id WHERE s.is_active AND a.is_active
        AND COALESCE(s.settings->>'exportable','true') <> 'false'
        AND (s.license_expires_at IS NULL OR s.license_expires_at>=current_date)
        ORDER BY s.key,a.key""")).mappings():
        fields.append({"key": row["attribute_key"], "source": row["key"],
                       "label": f"{row['key']} · {row['attribute_key']}",
                       "kind": "enrichment", "pii": True,
                       "sensitive": bool(row["sensitive"]) or sensitive_name(row["attribute_key"])})
    for field in fields:
        field.setdefault("sensitive", False)
    return [field for field in fields if role != "viewer" or not field["sensitive"]]


def sensitive_name(key):
    """Defense in depth even for legacy metadata predating sensitivity flags."""
    import re
    return bool(re.search(r"religio|politic|military|veteran", key.lower()))


def sensitive_fields(db, mapping):
    allowed = {(f["kind"], f.get("source"), f["key"]): f for f in registry(db)}
    selected = []
    for spec in mapping.values():
        field = allowed.get((spec.get("kind"), spec.get("source"), spec.get("key")))
        if field and field["sensitive"]:
            selected.append(field_reference(field))
    return sorted(set(selected))


def field_reference(field):
    return ":".join(str(part) for part in (field["kind"], field.get("source"), field["key"]) if part is not None)


def all_attributes(db, *, canonical_labels=False):
    """Generic all-attributes exports never implicitly opt into sensitive data."""
    result = {}
    for f in registry(db):
        if f["sensitive"]:
            continue
        name = (f["key"] if f["kind"] == "canonical" or f["key"] == "master_id"
                else f"computed__{f['kind']}__{f.get('source', '')}__{f['key']}") if canonical_labels else field_reference(f)
        if name in result:
            raise ValueError("Export column name collision.")
        result[name] = {key: f[key] for key in ("kind", "key", "source") if key in f}
    return result


def validate_mapping(db, mapping, include_pii=True, sensitive_selected=None):
    if not isinstance(mapping, dict) or not 1 <= len(mapping) <= 5000:
        raise ValueError("Choose between one and 5,000 export columns.")
    allowed = {(f["kind"], f.get("source"), f["key"]): f for f in registry(db)}
    for target, spec in mapping.items():
        if not isinstance(target, str) or not target.strip() or len(target) > 100 or any(ord(c) < 32 for c in target):
            raise ValueError("Column labels must be nonempty text without control characters.")
        if not isinstance(spec, dict) or set(spec) - {"kind", "key", "source", "value", "required"}:
            raise ValueError("Invalid field mapping.")
        if not isinstance(spec.get("kind"), str) or (
                spec.get("key") is not None and not isinstance(spec["key"], str)) or (
                spec.get("source") is not None and not isinstance(spec["source"], str)):
            raise ValueError("Mapping field names must be text.")
        if "required" in spec and not isinstance(spec["required"], bool):
            raise ValueError("Required-field flags must be boolean.")
        if spec.get("kind") == "constant":
            if not isinstance(spec.get("value"), str) or len(spec["value"]) > 2000:
                raise ValueError("Constant columns require text of at most 2,000 characters.")
            if not include_pii:
                raise ValueError("Constants are unavailable when excluding PII; use approved non-PII fields.")
            continue
        field = allowed.get((spec.get("kind"), spec.get("source"), spec.get("key")))
        if field is None:
            raise ValueError("An export field is unavailable, unlicensed or not exportable.")
        if field["sensitive"] and field_reference(field) not in (sensitive_selected or ()):
            raise ValueError("Sensitive export fields require explicit admin selection and approval.")
        if field["pii"] and not include_pii:
            raise ValueError("This destination excludes PII; remove contact and enrichment fields.")
    return mapping


def enrichments(db, ids):
    values = {}
    for row in db.execute(text("""SELECT e.*,s.key AS source_key FROM enrichment_values e
      JOIN sources s ON s.id=e.source_id
      WHERE e.profile_id=ANY(:ids) AND s.is_active
      AND COALESCE(s.settings->>'exportable','true') <> 'false'
      AND (s.license_expires_at IS NULL OR s.license_expires_at>=current_date)
      AND (e.license_expires_at IS NULL OR e.license_expires_at>=current_date)"""), {"ids": ids}).mappings():
        value = next((row[key] for key in ("value_text", "value_num", "value_bool", "value_date")
                      if row[key] is not None), None)
        values[(row["profile_id"], row["source_key"], row["attribute_key"])] = value
    return values


def mapped(row, mapping, enrichment):
    output = {}
    missing = False
    for target, spec in mapping.items():
        kind = spec["kind"]
        if kind == "profile":
            value = row.get(spec["key"])
        elif kind == "trait":
            value = (row.get("traits") or {}).get(spec["key"])
        elif kind == "canonical":
            value = (row.get("canonical") or {}).get(spec["key"])
        elif kind == "enrichment":
            value = enrichment.get((row["id"], spec["source"], spec["key"]))
        else:
            value = spec["value"]
        if value is None or value == "":
            missing |= bool(spec.get("required"))
        if isinstance(value, (list, dict)):
            import json
            value = json.dumps(value, separators=(",", ":"), default=str)
        elif value is not None and not isinstance(value, (int, float, bool, str)):
            value = str(value)
        output[target] = value
    return output, missing
