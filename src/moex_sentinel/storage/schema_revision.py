"""Read-only Alembic schema compatibility checks."""

from sqlalchemy import Engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory


def current_schema_revision(engine: Engine) -> str | None:
    """Return the applied revision without mutating the database."""
    if not inspect(engine).has_table("alembic_version"):
        return None
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    return None if revision is None else str(revision)


def expected_schema_revision(config_path: str = "alembic.ini") -> str:
    """Return the single revision head declared by the migration package."""
    head = ScriptDirectory.from_config(Config(config_path)).get_current_head()
    if head is None:
        raise RuntimeError("Alembic migration head is not configured.")
    return head


def schema_is_compatible(engine: Engine, expected_revision: str) -> bool:
    """Report exact schema-head compatibility."""
    return current_schema_revision(engine) == expected_revision
