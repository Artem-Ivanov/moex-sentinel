"""Behavioral checks for the isolated network/PostgreSQL profiling harness."""

import asyncio
import importlib
import os
from types import SimpleNamespace

import pytest

from develop.benchmarks.synthetic import SyntheticMarket, commands


def benchmark():
    return importlib.import_module("develop.benchmarks.postgresql")


@pytest.mark.parametrize("kwargs", [{"instruments": 0}, {"warmup": -1}, {"iterations": 0}, {"batch_size": 0}])
def test_invalid_workload_is_rejected_before_opening_database(kwargs):
    arguments = {"instruments": 1, "warmup": 0, "iterations": 1, "batch_size": 100} | kwargs
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(benchmark().run_case("postgresql+psycopg://unused.invalid/test", **arguments))


def test_non_postgresql_url_is_rejected():
    with pytest.raises(ValueError, match="PostgreSQL"):
        asyncio.run(benchmark().run_case("sqlite:///:memory:", 1))


@pytest.mark.postgresql
@pytest.mark.parametrize("batch_size", [2, 100])
def test_real_http_postgresql_profile_and_lost_ack_replay(batch_size):
    database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    result = asyncio.run(benchmark().run_case(database_url, 2, warmup=1, iterations=3, batch_size=batch_size))
    wait = result["wait"]
    assert wait["measured_decisions"] == 6
    assert wait["reason_counts"] == {"NO_THRESHOLD": 6}
    assert wait["decision_tick_ms"]["samples"] == 3
    for stage in (result["initial_buy_filled"], wait):
        for metric in ("queue_wait_ms", "http_roundtrip_ms", "core_ingress_ms", "sql_execution_ms", "dbapi_commit_ms"):
            sample = stage[metric]
            assert sample["samples"] > 0
            assert 0 <= sample["min"] <= sample["p50"] <= sample["p95"] <= sample["p99"] <= sample["max"]
        assert stage["http_roundtrip_ms"]["samples"] == stage["core_ingress_ms"]["samples"]
        assert stage["session_commit_including_flush_ms"]["samples"] == stage["dbapi_commit_ms"]["samples"]
        assert stage["facts_per_request"]["samples"] == stage["core_ingress_ms"]["samples"]
        assert stage["sql_execution_ms"]["samples"] == stage["sql_statement_count"]
        assert stage["queue_wait_ms"]["samples"] == stage["delivered_facts"]
        assert stage["pipeline_decisions_per_second"] > 0
    recovery = result["recovery"]
    assert recovery["lost_ack_transport_errors"] == 1
    assert recovery["backlog_before_restart"] == recovery["backlog_after_restart"] > 0
    assert recovery["backlog_final"] == 0
    assert recovery["replayed_facts"] == recovery["committed_before_restart"] > 0
    assert recovery["worker_decisions"] == recovery["core_decisions"]
    assert recovery["sdk_dispatches_before"] == recovery["sdk_dispatches_after"] == 2
    assert recovery["core_orders_before"] == recovery["core_orders_after"] == 2
    assert recovery["immutable_envelopes_preserved"] is True


def test_duplicate_dispatch_entry_is_counted_even_when_synthetic_broker_rejects():
    values = commands(1)
    broker = benchmark().CountedBroker(values, SyntheticMarket(values))
    broker.orders["already-filled"] = object()
    with pytest.raises(AssertionError, match="Duplicate dispatch"):
        asyncio.run(broker.dispatch_limit_order(SimpleNamespace(idempotency_key="already-filled")))
    assert broker.dispatch_calls == 1


def test_server_and_metrics_are_closed_when_http_client_close_fails(tmp_path):
    harness = benchmark().PostgreSQLHarness(tmp_path, 1, None, 100)
    closed = []

    def fail_close():
        raise RuntimeError("injected close failure")

    harness.delivery = SimpleNamespace(close=fail_close)
    harness.server = SimpleNamespace(close=lambda: closed.append("server"))
    harness.restore_metrics = lambda: closed.append("metrics")
    with pytest.raises(RuntimeError, match="injected close failure"):
        asyncio.run(harness.close())
    assert closed == ["server", "metrics"]
