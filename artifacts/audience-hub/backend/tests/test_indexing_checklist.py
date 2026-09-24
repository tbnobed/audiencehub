"""Verify DATA_MODEL.md's indexing checklist against the migrated PostgreSQL schema.

Run with the benchmark's --test-suite option; never connect to the application DB.
"""
import os
import re

import pytest
from sqlalchemy import text


def _normalize(sql):
    return re.sub(r"\s+", " ", sql.lower()).strip().removesuffix(" gin_trgm_ops")


def _index_inventory(connection):
    rows = connection.execute(text("""
        SELECT t.relname AS table_name, x.relname AS index_name,
               am.amname AS method, i.indisunique AS is_unique,
               i.indisvalid AS is_valid, i.indisready AS is_ready,
               ARRAY(
                   SELECT pg_get_indexdef(i.indexrelid, n, true)
                          || CASE WHEN (i.indoption[n - 1] & 1) = 1
                                  THEN ' DESC' ELSE '' END
                   FROM generate_series(1, i.indnkeyatts) AS n ORDER BY n
               ) AS keys,
               pg_get_expr(i.indpred, i.indrelid) AS predicate,
               ARRAY(
                   SELECT opc.opcname
                   FROM generate_series(0, i.indnkeyatts - 1) AS n
                   JOIN pg_opclass AS opc ON opc.oid = i.indclass[n]
                   ORDER BY n
               ) AS opclasses
        FROM pg_index AS i
        JOIN pg_class AS t ON t.oid = i.indrelid
        JOIN pg_namespace AS ns ON ns.oid = t.relnamespace
        JOIN pg_class AS x ON x.oid = i.indexrelid
        JOIN pg_am AS am ON am.oid = x.relam
        WHERE ns.nspname = current_schema()
    """)).mappings()
    return [dict(row) for row in rows]


def _matches(index, table, keys, *, unique=False, method="btree",
             predicate=None, opclasses=None):
    if (index["table_name"] != table or not index["is_valid"] or
            not index["is_ready"] or index["method"] != method or
            (unique and not index["is_unique"]) or
            tuple(_normalize(key) for key in index["keys"]) != keys):
        return False
    actual_predicate = index["predicate"]
    if predicate is None:
        if actual_predicate is not None:
            return False
    elif not predicate(actual_predicate or ""):
        return False
    return opclasses is None or tuple(index["opclasses"]) == opclasses


@pytest.mark.skipif(os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
                    reason="requires benchmark-owned disposable migrated PostgreSQL")
def test_migrated_indexing_checklist():
    from app.db import engine

    with engine.connect() as connection:
        indexes = _index_inventory(connection)
        partitions = connection.execute(text("""
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
            JOIN pg_namespace AS ns ON ns.oid = parent.relnamespace
            JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
            WHERE ns.nspname = current_schema() AND parent.relname = 'events'
              AND child.relkind = 'r'
            ORDER BY child.relname
        """)).scalars().all()

    # Each entry is a separate checklist requirement, including indexes backing
    # unique constraints and indexes automatically cloned onto event partitions.
    required = [
        ("identifiers", ("type", "value"), {"unique": True}),
        ("identifiers", ("profile_id",), {}),
        ("source_records", ("source_id", "external_id"), {"unique": True}),
        ("source_records", ("profile_id",), {}),
        ("source_records", ("id",), {
            "predicate": lambda p: re.sub(r"[()]", "", _normalize(p)) == "resolved_at is null",
        }),
        ("gifts", ("profile_id", "gift_date"), {}),
        ("gifts", ("gift_date",), {}),
        ("profiles", ("search_text",), {
            "method": "gin", "opclasses": ("gin_trgm_ops",),
        }),
        ("profiles", ("id",), {
            "predicate": lambda p: re.sub(r"[()]", "", _normalize(p)) == (
                "merged_into_id is null and not is_deleted"
            ),
        }),
        *[("profile_traits", (column,), {}) for column in (
            "donor_status", "last_gift_date", "ltv_total", "gift_amount_12m"
        )],
        ("enrichment_values", ("source_id", "attribute_key", "value_text"), {}),
        ("enrichment_values", ("source_id", "attribute_key", "value_num"), {}),
        ("segment_membership", ("profile_id",), {}),
    ]
    assert partitions, "Migrated events table has no physical partitions"
    for partition in partitions:
        required.extend([
            (partition, ("profile_id", "occurred_at desc"), {}),
            (partition, ("anonymous_id",), {}),
            (partition, ("name", "occurred_at"), {}),
        ])
    missing = [
        f"{table} ({', '.join(keys)}) {options}"
        for table, keys, options in required
        if not any(_matches(index, table, keys, **options) for index in indexes)
    ]
    assert not missing, "Missing or incorrect migrated indexes:\n" + "\n".join(missing)