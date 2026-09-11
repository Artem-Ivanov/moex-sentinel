import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import schema
from sqlalchemy.engine import URL, make_url

from moex_sentinel.storage.database import create_database_engine


@pytest.fixture
def postgresql_database_url() -> URL:
    database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    return make_url(database_url)


@pytest.fixture
def isolated_postgresql_database_url(postgresql_database_url: URL) -> Iterator[URL]:
    schema_name = f"test_{uuid4().hex}"
    admin_engine = create_database_engine(postgresql_database_url)
    with admin_engine.begin() as connection:
        connection.execute(schema.CreateSchema(schema_name))
    isolated_url = admin_engine.url.update_query_dict(
        {"options": f"-csearch_path={schema_name}"},
    )
    try:
        migration_config = Config("alembic.ini")
        rendered_url = isolated_url.render_as_string(hide_password=False).replace("%", "%%")
        migration_config.set_main_option("sqlalchemy.url", rendered_url)
        command.upgrade(migration_config, "head")
        yield isolated_url
    finally:
        with admin_engine.begin() as connection:
            connection.execute(schema.DropSchema(schema_name, cascade=True))
        admin_engine.dispose()
