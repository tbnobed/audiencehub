"""Transaction-owned, isolated COPY staging for set-based import writes."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session


@contextmanager
def staging_table(db: Session, columns: str) -> Iterator[str]:
    # A fresh name isolates concurrent jobs, including jobs sharing a source.
    # Creation/drop participate in the caller's transaction: rollback removes
    # the table even if COPY or an upsert fails.
    name = "ah_import_" + uuid4().hex
    db.execute(text(f"CREATE UNLOGGED TABLE {name} ({columns})"))
    try:
        yield name
    except BaseException:
        # An aborted PostgreSQL transaction cannot execute DROP; its rollback
        # also rolls back CREATE.
        raise
    else:
        db.execute(text(f"DROP TABLE {name}"))


def copy_upsert(
    db: Session, statement: str, rows: list[dict[str, Any]], columns: str,
    *, returning: bool = False,
) -> list[Any]:
    """COPY rows, then run a single INSERT SELECT.

    SQL uses ordinary :column placeholders for staging columns and {stage}
    for its FROM clause. Both SQL and column declarations are internal constants,
    never uploaded input.
    """
    if not rows:
        return []
    keys = list(rows[0])
    with staging_table(db, columns) as stage:
        raw = db.connection().connection.driver_connection
        with raw.cursor() as cursor:
            with cursor.copy(
                f"COPY {stage} ({', '.join(keys)}) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(tuple(row[key] for key in keys))
        sql = re.sub(r"(?<!:):([a-zA-Z_][a-zA-Z_0-9]*)",
                     lambda m: f'staged."{m[1]}"', statement)
        result = db.execute(text(sql.replace("{stage}", f"{stage} AS staged")))
        return result.all() if returning else []