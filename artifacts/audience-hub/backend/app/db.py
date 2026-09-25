import logging

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from app.config import get_settings


def database_url() -> str:
    url = get_settings().database_url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


engine = create_engine(database_url(), pool_pre_ping=True, hide_parameters=True)
logger = logging.getLogger(__name__)


def session_scope():
    session = Session(engine)
    # Session.close() can discard its transaction bookkeeping even when the
    # driver's rollback fails. Retain the actual handles until cleanup finishes
    # so that such a connection cannot go back into the pool.
    connections = set()

    def remember_connection(session, transaction, connection):
        connections.add(connection)

    event.listen(session, "after_begin", remember_connection)

    def cleanup():
        try:
            session.close()
        except BaseException:
            for connection in connections:
                if not connection.closed:
                    try:
                        connection.invalidate()
                    except BaseException:
                        logger.exception("Failed to invalidate database connection during cleanup")
            raise

    try:
        yield session
    except BaseException:
        try:
            cleanup()
        except BaseException:
            # A failed rollback must not turn an intentional HTTP error (or a
            # write failure) into an unrelated response from dependency teardown.
            logger.exception("Database cleanup failed while handling an earlier exception")
        raise
    else:
        # With no primary exception, transaction/cleanup failures remain visible.
        cleanup()
    finally:
        event.remove(session, "after_begin", remember_connection)