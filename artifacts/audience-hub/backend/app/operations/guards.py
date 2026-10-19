"""Explicit, database-bound authorization for synthetic production commands."""
import os
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy import text


def is_production():
    configured = dotenv_values(Path.cwd() / ".env").get("APP_ENV", "development")
    return (os.environ.get("APP_ENV") or configured).strip().lower() == "production"


def production_guard(engine, *, allow=False, database=None):
    if not is_production():
        return
    if not allow or not database:
        raise ValueError("Production command refused. Supply the explicit production flag AND --confirm-database with the exact database name.")
    with engine.connect() as connection:
        actual = connection.scalar(text("SELECT current_database()"))
    if database != actual:
        raise ValueError("Production command refused: typed database name does not match the connected database.")
