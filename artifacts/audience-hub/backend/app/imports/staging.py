"""Transaction-owned, isolated COPY staging for set-based import writes."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session


IMPORT_TABLES = frozenset({
    "source_records", "gifts", "events", "enrichment_attributes",
    "enrichment_values", "consents", "suppressions",
})


def analyze_tables(db: Session, tables) -> None:
    """Refresh only trusted target names, in the caller's transaction."""
    for table in sorted(set(tables)):
        if table not in IMPORT_TABLES:
            raise ValueError(f"Unsupported import statistics table: {table}")
        db.execute(text(f"ANALYZE {table}"))


class ImportStatistics:
    """Refresh touched targets at the import-wide written-row milestone."""

    def __init__(self):
        self.written: dict[str, int] = {}
        self.analyzed: set[str] = set()
        self.milestone_reached = False

    def record(self, db: Session, table: str, count: int) -> None:
        self.written[table] = self.written.get(table, 0) + max(0, count)
        if self.milestone_reached and table not in self.analyzed:
            analyze_tables(db, [table])
            self.analyzed.add(table)

    def advance(self, db: Session, accepted: int) -> None:
        """Called after each written batch, counting accepted import rows."""
        if accepted >= 50_000 and not self.milestone_reached:
            self.milestone_reached = True
            analyze_tables(db, self.written)
            self.analyzed.update(self.written)

    def finish(self, db: Session) -> None:
        analyze_tables(db, self.written)


import_statistics: ContextVar[ImportStatistics | None] = ContextVar(
    "import_statistics", default=None,
)


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
        statistics = import_statistics.get()
        if statistics is not None:
            target = re.search(r"INSERT\s+INTO\s+(\w+)", statement, re.IGNORECASE)
            if target and target[1] in IMPORT_TABLES:
                statistics.record(db, target[1], result.rowcount)
        return result.all() if returning else []