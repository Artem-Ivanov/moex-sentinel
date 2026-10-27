"""Function-scoped database resources for Core storage tests."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base


@pytest.fixture
def core_engine() -> Iterator[Engine]:
    """Yield a fresh in-memory schema and dispose its engine after the test."""
    engine = create_database_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def core_database(core_engine: Engine) -> Iterator[tuple[Engine, Session]]:
    """Close the test session before its owning engine fixture is disposed."""
    with Session(core_engine) as session:
        yield core_engine, session
