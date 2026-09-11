"""SQLite engine and transaction lifecycle."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker


def _ensure_database_directory(database_url: str | URL) -> None:
    database = make_url(database_url).database
    if database and database != ":memory:":
        Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_database_engine(
    database_url: str | URL,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout_seconds: float = 5.0,
) -> Engine:
    """Create a supported database engine with dialect-specific safety policy."""
    backend = make_url(database_url).get_backend_name()
    if backend not in {"sqlite", "postgresql"}:
        raise ValueError("Only SQLite or PostgreSQL database URLs are supported.")

    if backend == "postgresql":
        return create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout_seconds,
        )

    _ensure_database_directory(database_url)
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the sole session factory used by repositories."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit a short unit of work or roll it back atomically."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
