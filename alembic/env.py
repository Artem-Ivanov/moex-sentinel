"""Alembic environment for supported Core database dialects."""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from alembic import context
from moex_sentinel.storage.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """Allow CLI callers to override the configured disposable database."""
    cli_url = context.get_x_argument(as_dictionary=True).get("database_url")
    configured_url = cli_url or config.get_main_option("sqlalchemy.url")
    if configured_url is None:
        raise RuntimeError("Alembic requires a configured database URL.")
    return configured_url


def run_migrations_offline() -> None:
    configured_url = database_url()
    context.configure(
        url=configured_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=make_url(configured_url).get_backend_name() == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA busy_timeout=5000")
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=is_sqlite,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
