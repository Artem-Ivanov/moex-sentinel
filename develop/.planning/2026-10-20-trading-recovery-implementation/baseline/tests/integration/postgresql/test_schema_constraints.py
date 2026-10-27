"""Compare migrated constraints with PostgreSQL-normalized ORM reference DDL."""

from collections import Counter
from uuid import uuid4

import pytest
from sqlalchemy import inspect, schema

from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base


@pytest.fixture
def schema_inspectors(isolated_postgresql_database_url):
    migrated = create_database_engine(isolated_postgresql_database_url)
    reference_schema = f"reference_{uuid4().hex}"
    reference = create_database_engine(
        isolated_postgresql_database_url.update_query_dict({"options": f"-csearch_path={reference_schema}"})
    )
    try:
        with migrated.begin() as connection:
            connection.execute(schema.CreateSchema(reference_schema))
        Base.metadata.create_all(reference)
        yield inspect(migrated), inspect(reference)
    finally:
        reference.dispose()
        try:
            with migrated.begin() as connection:
                connection.execute(schema.DropSchema(reference_schema, cascade=True, if_exists=True))
        finally:
            migrated.dispose()


def foreign_keys(inspector, table):
    return Counter(
        (
            tuple(zip(item["constrained_columns"], item["referred_columns"], strict=True)),
            item["referred_table"],
            "local" if item["referred_schema"] == inspector.default_schema_name else item["referred_schema"],
            item["options"].get("ondelete", "NO ACTION"),
        )
        for item in inspector.get_foreign_keys(table, postgresql_ignore_search_path=True)
    )


def unique_constraints(inspector, table):
    return Counter(tuple(item["column_names"]) for item in inspector.get_unique_constraints(table))


def indexes(inspector, table):
    # PostgreSQL deparses both schemas, so casts/parentheses in source DDL do
    # not cause differences between equivalent migration and ORM predicates.
    return Counter(
        (
            tuple(item["column_names"]),
            item["unique"],
            item.get("dialect_options", {}).get("postgresql_where"),
        )
        for item in inspector.get_indexes(table)
        if not item.get("duplicates_constraint")
    )


@pytest.mark.postgresql
@pytest.mark.parametrize("signature", [foreign_keys, unique_constraints, indexes], ids=["fk", "unique", "indexes"])
def test_migrated_constraints_match_orm_semantics(schema_inspectors, signature):
    migrated, reference = schema_inspectors
    expected_tables = set(Base.metadata.tables)
    assert set(reference.get_table_names()) == expected_tables
    assert set(migrated.get_table_names()) - {"alembic_version"} == expected_tables
    for table in sorted(expected_tables):
        assert signature(migrated, table) == signature(reference, table), table
