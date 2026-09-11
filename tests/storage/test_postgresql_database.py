from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import Engine
from sqlalchemy.dialects import postgresql

import moex_sentinel.storage.database as database_module
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.types import UTCDateTime


def test_postgresql_engine_uses_configured_pool_policy(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected_engine = cast(Engine, object())

    def create_engine_spy(database_url: str, **options: Any) -> Engine:
        captured["database_url"] = database_url
        captured["options"] = options
        return expected_engine

    monkeypatch.setattr(database_module, "create_engine", create_engine_spy)

    engine = create_database_engine(
        "postgresql+psycopg://db-host/sentinel",
        pool_size=7,
        max_overflow=3,
        pool_timeout_seconds=2.5,
    )

    assert engine is expected_engine
    assert captured == {
        "database_url": "postgresql+psycopg://db-host/sentinel",
        "options": {
            "pool_pre_ping": True,
            "pool_size": 7,
            "max_overflow": 3,
            "pool_timeout": 2.5,
        },
    }


def test_utc_datetime_preserves_aware_utc_value_for_postgresql() -> None:
    column_type = UTCDateTime()
    original = datetime(2026, 8, 11, 10, 0, tzinfo=timezone(timedelta(hours=3)))

    bound = column_type.process_bind_param(original, postgresql.dialect())
    restored = column_type.process_result_value(bound, postgresql.dialect())

    assert bound == datetime(2026, 8, 11, 7, 0, tzinfo=UTC)
    assert restored == datetime(2026, 8, 11, 7, 0, tzinfo=UTC)
    assert restored is not None
    assert restored.tzinfo is UTC
