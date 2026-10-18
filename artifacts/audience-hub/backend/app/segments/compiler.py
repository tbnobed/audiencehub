"""Partner DSL compiler. All values are bound; columns are registry-owned.

Windows use UTC calendar dates, inclusive end dates. Missing scalar values do
not satisfy comparisons (including neq/not_in); explicit null rules do. Group
negation negates that two-valued result. Count/sum on absent facts is zero;
min/max is null. Percentile ties at the top-five-percent boundary are included.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError
from sqlalchemy import and_, or_, not_, select, func, literal, table, column, BigInteger, Text, Numeric, Date, DateTime, Boolean, cast
from app.models import (Profile, ProfileTrait, Gift, Event, Consent, EnrichmentValue,
                        Source, SegmentMembership, Segment)
from app.segments.registry import field_registry, OPERATORS
from app.segments.schema import Definition, SegmentValidationError, fail
from app.models import SourceRecord


def pinned_time(db, as_of=None):
    if as_of is None:
        as_of = db.scalar(select(func.current_timestamp()))
    if isinstance(as_of, str):
        try:
            as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            fail("as_of", "Expected an ISO date or timestamp.")
    if isinstance(as_of, date) and not isinstance(as_of, datetime):
        as_of = datetime.combine(as_of, time.min, timezone.utc)
    if not isinstance(as_of, datetime):
        fail("as_of", "Expected an ISO date or timestamp.")
    return as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of.astimezone(timezone.utc)


def _date(value, path):
    if not isinstance(value, str):
        fail(path, "Expected an ISO calendar date.")
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or not 1900 <= parsed.year <= 2999:
            raise ValueError()
        return parsed
    except ValueError:
        fail(path, "Expected an ISO calendar date.")


def _days(value, path):
    if type(value) is not int or not 1 <= value <= 36600:
        fail(path, "Days must be an integer between 1 and 36600.")
    return value


def _value(value, typ, path, enum_values=None):
    if typ in ("text", "enum"):
        if not isinstance(value, str) or len(value) > 4096:
            fail(path, "Expected text of at most 4096 characters.")
        if typ == "enum" and enum_values and value not in enum_values:
            fail(path, "Value is not in the allowed choices.")
        return value
    if typ == "number":
        if type(value) not in (int, float, Decimal):
            fail(path, "Expected a finite number.")
        try:
            result = Decimal(str(value))
            if not result.is_finite():
                raise InvalidOperation()
            return result
        except InvalidOperation:
            fail(path, "Expected a finite number.")
    if typ == "timestamp" and isinstance(value, str) and len(value) > 10:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not 1900 <= parsed.year <= 2999:
                raise ValueError()
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            fail(path, "Expected an ISO date or timestamp.")
    if typ in ("date", "timestamp"):
        return _date(value, path)
    fail(path, "Unsupported value type.")


def _boundary(day, typ):
    if isinstance(day, datetime):
        return day
    return datetime.combine(day, time.min, timezone.utc) if typ == "timestamp" else day


def _predicate(col, typ, node, path, now, enum_values=None):
    op = node.get("operator")
    if op not in OPERATORS[typ]:
        fail(path + ".operator", f"Unsupported operator for {typ}.")
    if op == "exists":
        return func.nullif(func.trim(cast(col, Text)), "").is_not(None)
    if op == "is_null":
        return col.is_(None)
    if op == "is_not_null":
        return col.is_not(None)
    if op in ("is_true", "is_false"):
        return col.is_(op == "is_true")
    if "value" not in node:
        fail(path + ".value", "A value is required.")
    value = node["value"]
    if op == "within_previous_days":
        days = _days(value, path + ".value")
        return and_(col >= _boundary(now.date() - timedelta(days=days), typ),
                    col < _boundary(now.date(), typ))
    if op in ("in_last_days", "not_in_last_days"):
        days = _days(value, path + ".value")
        expr = and_(col >= _boundary(now.date() - timedelta(days=days - 1), typ),
                    col < _boundary(now.date() + timedelta(days=1), typ))
        return and_(col.is_not(None), not_(expr)) if op == "not_in_last_days" else expr
    if op in ("between", "in", "not_in"):
        if not isinstance(value, list) or not value or len(value) > 200:
            fail(path + ".value", "Expected a nonempty list of at most 200 values.")
        vals = [_value(v, typ, f"{path}.value[{i}]", enum_values) for i, v in enumerate(value)]
        if op == "between":
            if len(vals) != 2 or (
                _boundary(vals[0], typ) > _boundary(vals[1], typ)
                if typ in ("date", "timestamp") else vals[0] > vals[1]
            ):
                fail(path + ".value", "Expected ordered start and end values.")
            if typ in ("date", "timestamp"):
                return and_(col >= _boundary(vals[0], typ),
                            col <= vals[1] if isinstance(vals[1], datetime)
                            else col < _boundary(vals[1] + timedelta(days=1), typ))
            return col.between(*vals)
        return col.in_(vals) if op == "in" else col.not_in(vals)
    val = _value(value, typ, path + ".value", enum_values)
    if op in ("before", "after"):
        if isinstance(val, datetime):
            return col < val if op == "before" else col > val
        return col < _boundary(val, typ) if op == "before" else col >= _boundary(val + timedelta(days=1), typ)
    if op in ("contains", "starts_with"):
        escaped = val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return col.like(("%" if op == "contains" else "") + escaped + "%", escape="\\")
    return {"eq": lambda: col == val, "neq": lambda: col != val,
            "gt": lambda: col > val, "gte": lambda: col >= val,
            "lt": lambda: col < val, "lte": lambda: col <= val}[op]()


def _window(col, typ, window, path, now):
    if window is None:
        return literal(True)
    if not isinstance(window, dict):
        fail(path, "Expected a time window.")
    kind = window.get("kind")
    if kind in ("previous_days", "previous_years"):
        if kind == "previous_days":
            if set(window) - {"kind", "days"}:
                fail(path, "Unknown window property.")
            start = now.date() - timedelta(days=_days(window.get("days"), path + ".days"))
        else:
            if set(window) != {"kind", "years"} or window["years"] != 5:
                fail(path, "Only a five-calendar-year window is supported.")
            from app.seed_dates import seed_window
            start = seed_window(now.date()).start
        return and_(col >= _boundary(start, typ), col < _boundary(now.date(), typ))
    if kind == "last_days":
        if set(window) - {"kind", "days"}:
            fail(path, "Unknown window property.")
        days = _days(window.get("days"), path + ".days")
        return and_(col >= _boundary(now.date() - timedelta(days=days - 1), typ),
                    col < _boundary(now.date() + timedelta(days=1), typ))
    if kind == "between":
        if set(window) - {"kind", "from", "to"}:
            fail(path, "Unknown window property.")
        return _predicate(col, typ, {"operator": "between", "value": [window.get("from"), window.get("to")]}, path, now)
    if kind in ("before", "after"):
        if set(window) - {"kind", "date"}:
            fail(path, "Unknown window property.")
        return _predicate(col, typ, {"operator": kind, "value": window.get("date")}, path, now)
    fail(path + ".kind", "Unknown window kind.")


class Compiler:
    def __init__(self, db, now):
        self.db, self.now, self.rules = db, now, 0
        # Value-picker DISTINCT scans belong to the catalog endpoint, not every
        # debounced count. Validation needs only whitelisted field metadata.
        self.registry = field_registry(db, include_values=False)["fields"]
        self.active = table("active_profiles", *[column(c.name, c.type) for c in Profile.__table__.columns])
        self.traits = ProfileTrait.__table__
        self.joins = self.active.outerjoin(self.traits, self.traits.c.profile_id == self.active.c.id)
        self.rank = None

    def node(self, node, path="root", depth=1):
        if not isinstance(node, dict):
            fail(path, "Expected a rule or group.")
        if depth > 5:
            fail(path, "Maximum nesting depth is five.")
        if "op" in node or "rules" in node:
            if set(node) - {"op", "rules", "negate"}:
                fail(path, "Unknown group property.")
            if node.get("op") not in ("and", "or") or not isinstance(node.get("rules"), list):
                fail(path, "A group needs and/or and a list of rules.")
            if not node["rules"]:
                fail(path + ".rules", "Add at least one rule.")
            if type(node.get("negate", False)) is not bool:
                fail(path + ".negate", "Expected a boolean.")
            children = [self.node(child, f"{path}.rules[{i}]", depth + 1) for i, child in enumerate(node["rules"])]
            expr = (and_ if node["op"] == "and" else or_)(*children)
            return not_(expr) if node.get("negate") else expr
        self.rules += 1
        if self.rules > 50:
            fail(path, "Maximum fifty rules.")
        kind = node.get("type")
        allowed = {
            "attribute": {"field", "operator", "value"},
            "profile": {"field", "operator", "value"}, "trait": {"field", "operator", "value"},
            "gift": {"aggregate", "operator", "value", "where", "window"},
            "event": {"aggregate", "operator", "value", "where", "window", "name"},
            "consent": {"channel", "status"}, "source": {"source_key", "operator"},
            "segment": {"segment_id", "operator"}, "enrichment": {"source", "field", "operator", "value"},
        }
        if not isinstance(kind, str) or kind not in allowed:
            fail(path + ".type", "Unknown rule type.")
        if set(node) - (allowed[kind] | {"type"}):
            fail(path, "Unknown rule property.")
        if kind in ("profile", "trait", "enrichment", "attribute"):
            entry = next((f for f in self.registry if f["type"] == kind and f["field"] == node.get("field")
                          and (kind != "enrichment" or f.get("source") == node.get("source"))), None)
            if entry is None:
                fail(path + ".field", "Unknown or unavailable field.")
            typ = entry["data_type"]
            if kind == "attribute":
                sr = SourceRecord.__table__
                key = node["field"]
                value = func.coalesce(sr.c[key], sr.c.attributes[key].astext) if key in ("address1", "address2", "city", "region", "postal_code", "country") else sr.c.attributes[key].astext
                col = select(value).join(Source, Source.id == sr.c.source_id).where(
                    sr.c.profile_id == self.active.c.id, value.is_not(None)
                ).order_by(Source.priority, sr.c.updated_at.desc(), sr.c.id).limit(1).correlate(self.active).scalar_subquery()
                col = cast(col if typ == "text" else func.nullif(func.trim(col), ""), {"text": Text, "number": Numeric, "date": Date,
                                 "timestamp": DateTime(timezone=True), "boolean": Boolean}[typ])
            elif kind == "profile":
                col = self.active.c[node["field"]]
            elif kind == "trait":
                if node["field"] == "top_5_percent_12m":
                    if self.rank is None:
                        donors = select(self.traits.c.profile_id,
                                        func.rank().over(order_by=self.traits.c.gift_amount_12m.desc()).label("rank"),
                                        func.count().over().label("population")).join(
                                            self.active, self.active.c.id == self.traits.c.profile_id
                                        ).where(self.traits.c.gift_amount_12m > 0).subquery()
                        self.rank = select(donors.c.profile_id).where(donors.c.rank <= func.ceil(donors.c.population * 0.05))
                    col = self.active.c.id.in_(self.rank)
                else:
                    col = self.traits.c[node["field"]]
            else:
                e = EnrichmentValue.__table__
                source_id = select(Source.id).where(Source.key == node["source"]).scalar_subquery()
                value_col = {"text": "value_text", "enum": "value_text", "number": "value_num",
                             "boolean": "value_bool", "date": "value_date"}[typ]
                col = select(e.c[value_col]).where(
                    e.c.profile_id == self.active.c.id, e.c.source_id == source_id,
                    e.c.attribute_key == node["field"],
                    or_(e.c.license_expires_at.is_(None), e.c.license_expires_at >= self.now.date())
                ).correlate(self.active).scalar_subquery()
            expr = _predicate(col, typ, node, path, self.now, entry["enum_values"])
        elif kind in ("gift", "event"):
            facts = Gift.__table__ if kind == "gift" else Event.__table__
            agg = node.get("aggregate")
            if agg not in (("count", "sum", "max", "min", "exists") if kind == "gift" else ("count", "exists")):
                fail(path + ".aggregate", "Unknown aggregate.")
            conds = [_window(facts.c.gift_date if kind == "gift" else facts.c.occurred_at,
                             "date" if kind == "gift" else "timestamp", node.get("window"), path + ".window", self.now)]
            if kind == "event":
                name = _value(node.get("name"), "text", path + ".name")
                if not name:
                    fail(path + ".name", "An event name is required.")
                conds.append(facts.c.name == name)
            where = node.get("where", {})
            if not isinstance(where, dict) or len(where) > 20:
                fail(path + ".where", "Expected at most twenty filters.")
            for key, rule in where.items():
                if not isinstance(rule, dict) or set(rule) - {"operator", "value"}:
                    fail(path + ".where." + key, "Invalid filter.")
                resource_keys = {"source_codes": "source_code", "products": "product_id", "vod_titles": "vod_title_id"}
                resource, _, field = key.partition(".")
                if resource in resource_keys:
                    if field not in ("key", "name", "series"):
                        fail(path + ".where." + key, "Unknown resource field.")
                    ref = table(resource, column("key", Text), column("name", Text), column("series", Text))
                    conds.append(select(literal(1)).select_from(ref).where(
                        ref.c.key == facts.c[resource_keys[resource]],
                        _predicate(ref.c[field], "text", rule, path + ".where." + key, self.now)
                    ).correlate(facts).exists())
                    continue
                if kind == "gift":
                    typ = {"fund": "text", "campaign": "text", "appeal_code": "text", "channel": "text",
                           "amount": "number", "is_recurring": "boolean"}.get(key)
                    if typ is None:
                        fail(path + ".where." + key, "Unknown gift filter.")
                    conds.append(_predicate(facts.c[key], typ, rule, path + ".where." + key, self.now))
                else:
                    if not key.startswith("properties.") or not key[11:] or len(key) > 200:
                        fail(path + ".where." + key, "Expected an event property.")
                    conds.append(_predicate(facts.c.properties[key[11:]].astext, "text", rule, path + ".where." + key, self.now))
            if agg == "exists":
                col = select(literal(1)).select_from(facts).where(facts.c.profile_id == self.active.c.id, *conds).correlate(self.active).exists()
                expr = _predicate(col, "boolean", node, path, self.now)
            else:
                aggregate = func.count() if agg == "count" else getattr(func, agg)(facts.c.amount)
                grouped = select(facts.c.profile_id, aggregate.label("value")).where(*conds).group_by(facts.c.profile_id).subquery()
                self.joins = self.joins.outerjoin(grouped, grouped.c.profile_id == self.active.c.id)
                col = func.coalesce(grouped.c.value, 0) if agg in ("count", "sum") else grouped.c.value
                expr = _predicate(col, "number", node, path, self.now)
        elif kind == "consent":
            channel = _value(node.get("channel"), "text", path + ".channel")
            if not channel:
                fail(path + ".channel", "Channel is required.")
            if node.get("status") not in ("opted_in", "opted_out", "unknown"):
                fail(path + ".status", "Unknown permission status.")
            status = select(Consent.status).where(Consent.profile_id == self.active.c.id,
                Consent.channel == channel).correlate(self.active).scalar_subquery()
            expr = func.coalesce(status, "unknown") == node["status"]
        elif kind == "source":
            key = _value(node.get("source_key"), "text", path + ".source_key")
            if node.get("operator") not in ("in", "not_in"):
                fail(path + ".operator", "Expected in or not_in.")
            contained = func.coalesce(self.traits.c.source_keys.contains([key]), False)
            expr = not_(contained) if node["operator"] == "not_in" else contained
        else:
            ident = node.get("segment_id")
            if type(ident) is not int or ident < 1:
                fail(path + ".segment_id", "Expected a positive segment ID.")
            status = self.db.scalar(select(Segment.status).where(Segment.id == ident))
            if status is None or status == "archived":
                fail(path + ".segment_id", "Referenced audience is unavailable.")
            if node.get("operator") not in ("in", "not_in"):
                fail(path + ".operator", "Expected in or not_in.")
            expr = self.active.c.id.in_(select(SegmentMembership.profile_id).where(SegmentMembership.segment_id == ident))
            if node["operator"] == "not_in":
                expr = not_(expr)
        return func.coalesce(expr, False)


def _compile(db, definition, as_of):
    try:
        parsed = Definition.model_validate(definition)
    except ValidationError as exc:
        raise SegmentValidationError([{"path": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]) from None
    if parsed.version != 1:
        fail("version", "Only definition version 1 is supported.")
    compiler = Compiler(db, pinned_time(db, as_of))
    expr = compiler.node(parsed.root)
    return select(compiler.active.c.id).select_from(compiler.joins).where(expr)


def compile_definition(db, definition, as_of=None):
    return _compile(db, definition, as_of)


def validate_definition(db, definition, as_of=None):
    _compile(db, definition, as_of)
    return deepcopy(definition)