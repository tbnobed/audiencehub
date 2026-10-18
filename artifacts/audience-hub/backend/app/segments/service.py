"""Transactional saved partner audiences and dependency-ordered membership."""
from datetime import datetime, timezone
from sqlalchemy import text, select
from app.models import Segment, AuditLog
from app.jobs.queue import enqueue
from app.segments.compiler import compile_definition, validate_definition

GRAPH_LOCK = 73192304


def references(definition):
    result = set()
    def visit(node):
        if "rules" in node:
            for child in node["rules"]:
                visit(child)
        elif node.get("type") == "segment":
            result.add(int(node["segment_id"]))
    visit(definition["root"])
    return result


def graph_lock(db):
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": GRAPH_LOCK})


def validate_references(db, segment_id, definition, status):
    graph_lock(db)
    rows = {s.id: s for s in db.scalars(select(Segment)).all()}
    graph = {key: references(row.definition) for key, row in rows.items()}
    graph[segment_id] = references(definition)
    for target in graph[segment_id]:
        row = rows.get(target)
        if row is None or row.status == "archived":
            raise ValueError("Referenced partner audience does not exist or is archived.")
        if status == "active" and row.status != "active":
            raise ValueError("Active audiences can reference only active audiences.")
    visiting, done = set(), set()
    def visit(key):
        if key in visiting:
            raise ValueError("Partner audience references cannot form a cycle.")
        if key in done:
            return
        visiting.add(key)
        for child in graph.get(key, ()):
            visit(child)
        visiting.remove(key)
        done.add(key)
    visit(segment_id)


def request_materialization(db, segment_id):
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise ValueError("Partner audience not found.")
    # A save waiting behind an in-flight refresh must not dedupe against that
    # old job before the worker records completion in its next transaction.
    revision = segment.updated_at.isoformat()
    return enqueue(db, "segment.materialize", {"segment_id": segment_id},
                   dedupe_key=f"segment.materialize:{segment_id}:{revision}")


def materialize(db, segment_id, as_of=None):
    """Caller commits; graph lock serializes saves, refresh and archive.

    Queued jobs intentionally read the latest committed definition, not a copied
    payload. A changed definition queues a separately deduped revision.
    """
    from app.identity.locking import IdentityResolutionDeferred
    if not db.scalar(text(
        "SELECT pg_try_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"
    )):
        raise IdentityResolutionDeferred("Partner audience refresh waits for traits.")
    graph_lock(db)
    as_of = as_of or datetime.now(timezone.utc)
    visiting, done = set(), set()
    def refresh(key):
        if key in visiting:
            raise ValueError("Partner audience dependency cycle.")
        if key in done:
            return
        segment = db.get(Segment, key)
        if segment is None or segment.status != "active":
            raise ValueError("Only active partner audiences can be materialized.")
        visiting.add(key)
        for dependency in sorted(references(segment.definition)):
            refresh(dependency)
        query = compile_definition(db, segment.definition, as_of)
        db.execute(text("CREATE TEMP TABLE IF NOT EXISTS segment_refresh_ids (profile_id bigint PRIMARY KEY) ON COMMIT DROP"))
        db.execute(text("TRUNCATE segment_refresh_ids"))
        from sqlalchemy import table, column, BigInteger, insert
        target = table("segment_refresh_ids", column("profile_id", BigInteger))
        db.execute(insert(target).from_select(["profile_id"], query))
        removed = db.execute(text("""
          DELETE FROM segment_membership m WHERE segment_id=:id
          AND NOT EXISTS(SELECT 1 FROM segment_refresh_ids t WHERE t.profile_id=m.profile_id)
        """), {"id": key}).rowcount
        added = db.execute(text("""
          INSERT INTO segment_membership(segment_id,profile_id)
          SELECT :id,profile_id FROM segment_refresh_ids ON CONFLICT DO NOTHING
        """), {"id": key}).rowcount
        count = db.scalar(text("SELECT count(*) FROM segment_refresh_ids"))
        db.execute(text("""INSERT INTO segment_counts(segment_id,count,added,removed)
          VALUES(:id,:count,:added,:removed)"""),
          {"id": key, "count": count, "added": added, "removed": removed})
        segment.last_count = count
        segment.last_materialized_at = datetime.now(timezone.utc)
        db.add(AuditLog(actor_type="system", action="segment.materialized",
                        entity_type="segment", entity_id=str(key),
                        details={"count": count, "added": added, "removed": removed}))
        db.flush()
        visiting.remove(key)
        done.add(key)
    refresh(segment_id)
    return db.get(Segment, segment_id).last_count


def materialize_for_activation(db, segment_id, as_of=None):
    """Future activation callers refresh dependencies in their transaction."""
    return materialize(db, segment_id, as_of)


def ensure_templates(db, definitions=None):
    from app.segments.templates import template_definitions, legacy_template_definitions
    graph_lock(db)
    if definitions is None:
        legacy = {t["key"]: t for t in legacy_template_definitions()}
        available = [t for t in template_definitions() if db.scalar(
            select(Segment.id).where(Segment.template_key == t["key"])) is None]
        referenced = set()
        for definition in db.scalars(select(Segment.definition)):
            referenced.update(references(definition))
        for saved in db.scalars(select(Segment).where(Segment.template_key.in_(legacy))):
            original = legacy[saved.template_key]
            # Reuse only untouched, never-materialized bootstrap drafts. Anything
            # edited, activated, owned or referenced remains a normal saved audience.
            untouched = (
                saved.status == "draft" and saved.created_by is None
                and saved.last_materialized_at is None and saved.id not in referenced
                and saved.name == original["name"] and saved.description == original["description"]
                and saved.definition == original["definition"]
            )
            saved.template_key = None
            if untouched and available:
                replacement = available.pop(0)
                saved.template_key = replacement["key"]
                saved.name = replacement["name"]
                saved.description = replacement["description"]
                saved.definition = replacement["definition"]
        db.flush()
    for template in definitions if definitions is not None else template_definitions():
        if db.scalar(select(Segment.id).where(Segment.template_key == template["key"])) is None:
            db.add(Segment(template_key=template["key"], name=template["name"],
                           description=template["description"], definition=template["definition"],
                           status="draft", refresh_schedule="daily"))
    db.flush()