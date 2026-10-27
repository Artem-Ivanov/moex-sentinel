"""Explicit schema migration command for deployment jobs."""

import json
import sys
from collections.abc import Sequence

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from alembic import command
from alembic.config import Config


class MigrationSettings(BaseSettings):
    """Database configuration for the schema job, independent of Core runtime."""

    model_config = SettingsConfigDict(
        env_file="../../.env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = "sqlite:///data/moex_sentinel.db"

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if make_url(value).get_backend_name() not in {"sqlite", "postgresql"}:
            raise ValueError("Core schema supports only SQLite and PostgreSQL.")
        return value


def main(argv: Sequence[str] | None = None) -> int:
    """Upgrade the configured Core schema and emit only a safe status."""
    if argv:
        raise ValueError("Schema migration command does not accept positional arguments.")
    try:
        settings = MigrationSettings()
        alembic_config = Config("alembic.ini")
        alembic_config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
        command.upgrade(alembic_config, "head")
    except Exception:
        sys.stderr.write(f'{json.dumps({"error": "SCHEMA_MIGRATION_FAILED"}, sort_keys=True)}\n')
        return 1
    sys.stdout.write(f'{json.dumps({"status": "SCHEMA_UPGRADED"}, sort_keys=True)}\n')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
