from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.config import get_settings


def database_url() -> str:
    url = get_settings().database_url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


engine = create_engine(database_url(), pool_pre_ping=True)


def session_scope():
    with Session(engine) as session:
        yield session