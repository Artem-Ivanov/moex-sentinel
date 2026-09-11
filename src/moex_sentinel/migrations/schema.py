"""Explicit schema migration command for deployment jobs."""

import json
import sys
from collections.abc import Sequence

from alembic import command
from alembic.config import Config
from moex_sentinel.config import Settings


def main(argv: Sequence[str] | None = None) -> int:
    """Upgrade the configured Core schema and emit only a safe status."""
    if argv:
        raise ValueError("Schema migration command does not accept positional arguments.")
    try:
        settings = Settings()
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
