import logging

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from app.config import get_settings
from app.jobs import lifecycle


def database_url() -> str:
    url = get_settings().database_url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


CONNECT_ARGS = {
    "application_name": lifecycle.default_name(),
    "keepalives": 1, "keepalives_idle": 30,
    "keepalives_interval": 10, "keepalives_count": 3,
    "connect_timeout": 5,
    "options": "-c idle_in_transaction_session_timeout="
               f"{get_settings().idle_transaction_timeout_seconds * 1000}"
               f" -c statement_timeout={get_settings().api_statement_timeout_seconds * 1000}"
               " -c tcp_keepalives_idle=30 -c tcp_keepalives_interval=10"
               " -c tcp_keepalives_count=3",
}
engine = create_engine(database_url(), pool_pre_ping=True, hide_parameters=True,
                       connect_args=CONNECT_ARGS)
# Cancellation must never wait for a slot in a handler-exhausted pool.
control_engine = create_engine(database_url(), poolclass=NullPool, hide_parameters=True,
                               connect_args=CONNECT_ARGS)
logger = logging.getLogger(__name__)


@event.listens_for(engine, "checkout")
@event.listens_for(control_engine, "checkout")
def identify_connection(connection, _record, _proxy):
    owner = lifecycle.current()
    if owner and owner.cancelled.is_set():
        raise lifecycle.OwnershipLost("Worker is stopping")
    try:
        with connection.cursor() as cursor:
            name = owner.name if owner else lifecycle.default_name()
            if owner:
                # Bound registration itself, including older pooled settings.
                # A recovering transaction can hold this lock until commit.
                cursor.execute("SET LOCAL statement_timeout='5s'")
                # Registration and stale recovery serialize. Tag BEFORE checking
                # ownership; a recovering transaction cannot miss a late checkout.
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,73519))", (name,))
            cursor.execute("SELECT set_config('application_name',%s,false), "
                           "set_config('statement_timeout',%s,false)",
                           (name, str(lifecycle.timeout_ms(owner.job_type if owner else None))))
            if owner:
                cursor.execute("SELECT 1 FROM jobs WHERE id=%s AND status='running' AND started_at=%s",
                               (owner.job_id, owner.started_at))
                if not cursor.fetchone() or owner.cancelled.is_set():
                    raise lifecycle.OwnershipLost("Job no longer owns this attempt")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


@event.listens_for(control_engine, "checkout")
def bound_control_queries(connection, _record, _proxy):
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout='5s'")
    connection.commit()


@event.listens_for(engine, "before_cursor_execute")
def reject_stopping_sql(_conn, _cursor, _statement, _parameters, _context, _many):
    owner = lifecycle.current()
    if owner and owner.cancelled.is_set():
        raise lifecycle.OwnershipLost("Worker is stopping")


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