"""Public field catalog: SQL identifiers never come from client input."""
from sqlalchemy import select
from app.models import EnrichmentAttribute, Gift, Source
from app.traits.registry import TRAITS

OPERATORS = {
    "text": ["eq", "neq", "in", "not_in", "contains", "starts_with", "is_null", "is_not_null"],
    "number": ["eq", "neq", "gt", "gte", "lt", "lte", "between", "is_null", "is_not_null"],
    "date": ["before", "after", "between", "in_last_days", "not_in_last_days", "is_null", "is_not_null"],
    "timestamp": ["before", "after", "between", "in_last_days", "not_in_last_days", "is_null", "is_not_null"],
    "boolean": ["is_true", "is_false"],
    "enum": ["in", "not_in"],
}
PROFILE_FIELDS = {
    **{k: "text" for k in ("email", "phone", "first_name", "last_name", "address1", "city", "region", "postal_code", "country")},
    "id": "number", "first_seen_at": "timestamp", "last_seen_at": "timestamp",
}
TYPE_MAP = {"int": "number", "numeric": "number", "smallint": "number", "bool": "boolean",
            "timestamptz": "timestamp", "text": "text", "date": "date"}
STATUSES = ["prospect", "new", "active", "reactivated", "lapsing", "lapsed"]


def field_registry(db, *, include_values=True):
    fields = []
    def add(kind, field, data_type, label=None, group=None, **extra):
        fields.append(dict(key=f"{kind}.{extra.get('source', '') + '.' if extra.get('source') else ''}{field}",
                           type=kind, field=field, data_type=data_type,
                           label=label or field.replace("_", " ").capitalize(),
                           group=group or "Partners", operators=OPERATORS[data_type],
                           enum_values=[], **extra))
    for key, typ in PROFILE_FIELDS.items():
        add("profile", key, typ)
    for trait in TRAITS:
        if trait.key == "source_keys":
            continue
        typ = "enum" if trait.key == "donor_status" else TYPE_MAP[trait.type]
        add("trait", trait.key, typ, trait.label.replace("donor", "partner").replace("Donor", "Partner"),
            "Giving" if "gift" in trait.key or trait.key.startswith(("rfm", "ltv")) else "Engagement")
        if trait.key == "donor_status":
            fields[-1]["enum_values"] = STATUSES
    add("trait", "top_5_percent_12m", "boolean", "Top 5% by 12-month giving", "Giving")
    for aggregate in ("count", "sum", "max", "min", "exists"):
        add("gift", aggregate, "boolean" if aggregate == "exists" else "number",
            f"Gift {aggregate}", "Giving", aggregate=aggregate)
    for aggregate in ("count", "exists"):
        add("event", aggregate, "boolean" if aggregate == "exists" else "number",
            f"Event {aggregate}", "Engagement", aggregate=aggregate)
    add("consent", "email", "enum", "Email permission", "Consent", channel="email")
    fields[-1]["enum_values"] = ["opted_in", "opted_out", "unknown"]
    sources = list(db.execute(select(Source.key, Source.name)).all())
    add("source", "source_key", "enum", "Source", "Sources")
    fields[-1]["enum_values"] = [row.key for row in sources]
    add("segment", "segment_id", "enum", "Saved audience", "Segments")
    for attr, source in db.execute(select(EnrichmentAttribute, Source).join(
            Source, Source.id == EnrichmentAttribute.source_id).where(EnrichmentAttribute.is_active)):
        typ = {"num": "number", "number": "number", "bool": "boolean"}.get(attr.data_type, attr.data_type)
        add("enrichment", attr.key, typ, attr.label, source.name, source=source.key)
        fields[-1]["enum_values"] = attr.enum_values or []
    gift_values = {}
    if include_values:
        for key in ("fund", "campaign", "appeal_code", "channel"):
            col = getattr(Gift, key)
            gift_values[key] = list(db.scalars(select(col).where(col.is_not(None)).distinct().order_by(col).limit(200)))
    return {"fields": fields, "gift_values": gift_values}