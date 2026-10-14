import os

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL

from develop.benchmarks.portfolio_run_index import isolated_database, local_test_url


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///:memory:",
        "postgresql+psycopg://u:p@example.org:55433/test",
        "postgresql+psycopg://u:p@127.0.0.1:5432/test",
        "postgresql+psycopg://u:p@127.0.0.1:55433/test?host=example.org",
        "postgresql+psycopg://u:p@127.0.0.1:55433/test?hostaddr=203.0.113.1",
        "postgresql+psycopg://u:p@127.0.0.1:55433/test?port=5432",
        "postgresql+psycopg://u:p@127.0.0.1:55433/test?options=-csearch_path=public",
    ],
)
def test_benchmark_rejects_unsafe_database_before_connection(url):
    with pytest.raises(ValueError, match="dedicated local test PostgreSQL"):
        local_test_url(url)


def test_benchmark_accepts_only_assigned_local_endpoint_without_options():
    url = URL.create("postgresql+psycopg", username="synthetic", host="127.0.0.1", port=55433, database="test")
    assert local_test_url(url) == url


@pytest.mark.postgresql
def test_benchmark_cleans_owned_schema_after_failure():
    url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    admin = create_engine(local_test_url(url))

    def fail_in_owned_schema():
        with isolated_database(url) as engine, engine.connect() as connection:
            assert connection.dialect.name == "postgresql"
            assert len(set(inspect(admin).get_schema_names()) - before) == 1
            raise RuntimeError("synthetic failure")

    try:
        before = set(inspect(admin).get_schema_names())
        with pytest.raises(RuntimeError, match="synthetic failure"):
            fail_in_owned_schema()
        assert set(inspect(admin).get_schema_names()) == before
    finally:
        admin.dispose()
