"""Add historical monthly event partitions and route existing rows.

Revision ID: 0003_historical_event_partitions
Revises: 0002_milestone_two
"""
from datetime import datetime, timezone
import re

from alembic import op
import sqlalchemy as sa

revision = "0003_historical_event_partitions"
down_revision = "0002_milestone_two"
branch_labels = None
depends_on = None

_FIRST_MONTH = datetime(2020, 1, 1, tzinfo=timezone.utc)
_OWNERSHIP_COMMENT = "created by Alembic revision 0003_historical_event_partitions"


def _shift_month(value, delta):
    month = value.month - 1 + delta
    return value.replace(year=value.year + month // 12, month=month % 12 + 1)


def _monthly_partitions():
    rows = op.get_bind().execute(sa.text("""
        SELECT child.relname
        FROM pg_inherits
        JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        JOIN pg_class child ON child.oid = pg_inherits.inhrelid
        WHERE parent.relname = 'events'
          AND parent_ns.nspname = current_schema()
          AND child.relname ~ '^events_[0-9]{4}_[0-9]{2}$'
    """))
    partitions = []
    for (name,) in rows:
        match = re.fullmatch(r"events_(\d{4})_(\d{2})", name)
        if match:
            partitions.append(datetime(int(match.group(1)), int(match.group(2)), 1,
                                       tzinfo=timezone.utc))
    return partitions


def upgrade():
    existing = _monthly_partitions()
    first_existing = min(existing) if existing else None
    if first_existing is None or first_existing > _FIRST_MONTH:
        end_month = first_existing
        if end_month is None:
            raise RuntimeError("Could not find the existing monthly events partition boundary")

        start_sql = _FIRST_MONTH.isoformat()
        end_sql = end_month.isoformat()
        op.execute(sa.text(f"""
            CREATE TEMP TABLE events_0003_upgrade_rows ON COMMIT DROP AS
            SELECT * FROM events_default
            WHERE occurred_at >= '{start_sql}' AND occurred_at < '{end_sql}'
        """))
        op.execute(sa.text(f"""
            DELETE FROM events_default
            WHERE occurred_at >= '{start_sql}' AND occurred_at < '{end_sql}'
        """))

        month = _FIRST_MONTH
        while month < end_month:
            following = _shift_month(month, 1)
            if month not in existing:
                partition_name = f"events_{month.year:04d}_{month.month:02d}"
                op.execute(
                    f"CREATE TABLE {partition_name} PARTITION OF events "
                    f"FOR VALUES FROM ('{month.isoformat()}') TO ('{following.isoformat()}')"
                )
                op.execute(
                    f"COMMENT ON TABLE {partition_name} IS '{_OWNERSHIP_COMMENT}'"
                )
            month = following

        op.execute("""
            INSERT INTO events OVERRIDING SYSTEM VALUE
            SELECT * FROM events_0003_upgrade_rows
        """)


def downgrade():
    bind = op.get_bind()
    rows = bind.execute(sa.text("""
        SELECT child.relname
        FROM pg_inherits
        JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        JOIN pg_class child ON child.oid = pg_inherits.inhrelid
        WHERE parent.relname = 'events'
          AND parent_ns.nspname = current_schema()
          AND child.relname ~ '^events_[0-9]{4}_[0-9]{2}$'
          AND obj_description(child.oid, 'pg_class') = :ownership_comment
        ORDER BY child.relname
    """), {"ownership_comment": _OWNERSHIP_COMMENT})
    partitions = [name for (name,) in rows]
    if not partitions:
        return

    op.execute("""
        CREATE TEMP TABLE events_0003_downgrade_rows ON COMMIT DROP AS
        SELECT * FROM events WITH NO DATA
    """)
    for partition_name in partitions:
        op.execute(
            f"INSERT INTO events_0003_downgrade_rows "
            f"SELECT * FROM {partition_name}"
        )
        op.execute(f"DROP TABLE {partition_name}")

    op.execute("""
        INSERT INTO events OVERRIDING SYSTEM VALUE
        SELECT * FROM events_0003_downgrade_rows
    """)