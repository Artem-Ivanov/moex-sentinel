"""Validate the disposable full HTTP/PostgreSQL backlog measurement."""

import asyncio
import os

import pytest

from develop.benchmarks.postgresql_backlog import run_case


@pytest.mark.postgresql
def test_audit_backlog_drains_over_http_into_isolated_postgresql():
    database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    result = asyncio.run(run_case(database_url, 100, batch_size=10))
    assert result["initial_backlog"] == 100
    assert result["worker_remaining"] == 0
    assert result["core_events"] == 100
    assert result["core_audits"] == 100
    assert result["core_sequence"] == result["worker_sequence"] == 100
    assert result["core_revision"] == result["worker_revision"] == 1
    assert result["requests"] == 10
    assert "delivered_facts" not in result["core_profile"]
