"""SQL and commit attribution uses the request's bucket under real overlap."""

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import httpx
import pytest
from sqlalchemy import create_engine, event, text, update
from sqlalchemy.orm import sessionmaker

from develop.benchmarks import core_http_concurrency as benchmark
from develop.benchmarks.pg_metrics import Profile, instrument_database
from develop.benchmarks.pg_server import TimedIngress
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base, TradingAutomationModel


def test_overlapping_requests_have_independent_sql_commit_counts_including_failed_request(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'metrics.db'}")
    profile = Profile()
    session_class, restore = instrument_database(engine, profile)
    factory = sessionmaker(engine, class_=session_class)
    barrier = Barrier(2)

    class Ingress:
        def publish(self, facts):
            count = len(facts)
            with factory() as session:
                session.scalar(text("SELECT 1"))
                barrier.wait(timeout=3)
                for _ in range(count - 1):
                    session.scalar(text("SELECT 1"))
                if count == 3:
                    raise ValueError("synthetic request failure")
                session.commit()
            return count

    ingress = TimedIngress(Ingress(), profile, require_all=False)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ingress.publish, (1, 2))
            failed = pool.submit(ingress.publish, (1, 2, 3))
            assert first.result(timeout=5) == 2
            with pytest.raises(ValueError, match="synthetic"):
                failed.result(timeout=5)
        assert sorted(profile.samples["sql_statements_per_request"]) == [2, 3]
        assert sorted(profile.samples["dbapi_commits_per_request"]) == [0, 1]
        assert sorted(profile.samples["facts_per_request"]) == [2, 3]
        assert len(profile.samples["sql_execution_ms"]) == 5
        with factory() as session:
            session.scalar(text("SELECT 1"))
            session.commit()
        assert len(profile.samples["sql_execution_ms"]) == 5
    finally:
        restore()
        engine.dispose()


def test_failed_commit_retains_attempt_count_and_resets_request_bucket(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'commit.db'}")
    profile = Profile()

    def fail_commit(_connection):
        raise ValueError("synthetic commit failure")

    engine.dialect.do_commit = fail_commit
    session_class, restore = instrument_database(engine, profile)
    factory = sessionmaker(engine, class_=session_class)

    class Ingress:
        def publish(self, _facts):
            with factory() as session:
                session.scalar(text("SELECT 1"))
                session.commit()

    try:
        with pytest.raises(ValueError, match="synthetic commit"):
            TimedIngress(Ingress(), profile, require_all=False).publish((1,))
        assert profile.samples["sql_statements_per_request"] == [1]
        assert profile.samples["dbapi_commits_per_request"] == [1]
        assert len(profile.samples["dbapi_commit_ms"]) == 1
        with factory() as session:
            session.scalar(text("SELECT 1"))
        assert len(profile.samples["sql_execution_ms"]) == 1
    finally:
        restore()
        engine.dispose()


@pytest.mark.parametrize("url_suffix", ["?host=remote.invalid", "?port=5432", "?dbname=other_database", "driver"])
def test_http_benchmark_rejects_dialect_and_query_overrides_before_engine(tmp_path, monkeypatch, url_suffix):
    calls = []
    driver = "postgresql+psycopg2" if url_suffix == "driver" else "postgresql+psycopg"
    query = "" if url_suffix == "driver" else url_suffix
    url = f"{driver}://sentinel_test:synthetic@127.0.0.1:60580/sentinel_core_performance_test{query}"
    monkeypatch.setattr(benchmark, "measure", lambda *_args: calls.append("engine"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark", "--database-url", url, "--mode", "bounded", "--output", str(tmp_path / "receipt.json")],
    )
    with pytest.raises(AssertionError, match="Only the assigned"):
        benchmark.main()
    assert calls == []


def test_http_benchmark_records_transport_json_cancel_and_success_for_every_scheduled_index(monkeypatch):
    facts = benchmark.facts_for(benchmark.commands(1))
    samples, calls = [], []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            assert len(samples) == 4, "Client closed before every request reached a terminal receipt"

        async def post(self, *_args, **_kwargs):
            index = len(calls)
            calls.append(index)
            await asyncio.sleep(0)
            if index == 0:
                raise httpx.ReadError("synthetic transport failure")
            if index == 1:
                return httpx.Response(200, text="synthetic non-JSON response")
            if index == 2:
                raise asyncio.CancelledError
            return httpx.Response(
                200, json={"failures": [], "results": [{"accepted_event_ids": [str(facts[0].event_id)]}]}
            )

    monkeypatch.setattr(benchmark.httpx, "AsyncClient", Client)
    with pytest.raises(AssertionError, match="failed or cancelled"):
        asyncio.run(benchmark.workload("http://unused.invalid", facts, 4, 2, samples))
    assert sorted(sample["index"] for sample in samples) == [0, 1, 2, 3]
    by_index = {sample["index"]: sample for sample in samples}
    assert [by_index[index]["state"] for index in range(4)] == ["failed", "failed", "cancelled", "passed"]
    assert by_index[0]["exception_type"] == "ReadError"
    assert by_index[1]["exception_type"] == "JSONDecodeError"
    assert by_index[1]["response_text"] == "synthetic non-JSON response"
    assert by_index[2]["exception_type"] == "CancelledError"
    assert len(calls) == 4


@pytest.mark.parametrize(("workload_failure", "diagnostic_failure"), [(True, False), (True, True), (False, True)])
def test_http_benchmark_failure_records_actual_persisted_state_after_server_drain(
    tmp_path, monkeypatch, workload_failure, diagnostic_failure
):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'failed-receipt.db'}")
    Base.metadata.create_all(engine)
    servers, lifecycle = [], []
    diagnostic_blocked = [False]

    @event.listens_for(engine, "before_cursor_execute")
    def failed_diagnostic(*_args):
        if diagnostic_blocked[0]:
            raise ValueError("synthetic diagnostic read failure")

    @contextmanager
    def isolated(_url):
        try:
            yield engine
        finally:
            lifecycle.append("schema-owner-close")
            engine.dispose()

    class Server:
        def __init__(self, ingress, _profile, **_kwargs):
            self.ingress = ingress
            self.url = "http://unused.invalid"
            servers.append(self)

        def start(self):
            pass

        def close(self):
            # Simulate a companion request completing while the server drains.
            with engine.begin() as connection:
                connection.execute(
                    update(TradingAutomationModel)
                    .where(TradingAutomationModel.id == str(benchmark.commands(2)[0].automation_id))
                    .values(revision=7, last_sequence_number=6)
                )
            lifecycle.append("server-drained")
            diagnostic_blocked[0] = diagnostic_failure

    calls = []

    async def workload(_url, facts, _requests, _concurrency, samples):
        calls.append((_requests, _concurrency))
        result = servers[0].ingress.publish(facts)
        assert result.failures == ()
        if len(calls) == 2:
            samples.append({"index": 0, "state": "failed" if workload_failure else "passed"})
            if workload_failure:
                samples[0]["exception_type"] = "ReadError"
                raise httpx.ReadError("synthetic response loss after committed work")

    monkeypatch.setattr(benchmark, "isolated_database", isolated)
    monkeypatch.setattr(benchmark, "CoreServer", Server)
    monkeypatch.setattr(benchmark, "workload", workload)
    receipt = {"warmup_samples": [], "samples": []}
    expected_error = httpx.ReadError if workload_failure else ValueError
    with pytest.raises(expected_error, match="synthetic"):
        benchmark.measure("unused", "bounded", 2, 1, 4, receipt)
    assert calls == [(4, 4), (1, 4)]
    assert lifecycle == ["server-drained", "schema-owner-close"]
    if diagnostic_failure:
        assert receipt["diagnostic_error_type"] == "ValueError"
        assert receipt["persisted_events"] is None
        assert receipt["final_automations"] is None
    else:
        assert receipt["persisted_events"] == 2
        assert sorted((row["revision"], row["last_sequence_number"]) for row in receipt["final_automations"]) == [
            (2, 1),
            (7, 6),
        ]
    assert receipt["final_counts_verified"] is False
    assert receipt["scheduled_requests"] == 1
    assert receipt["successful_requests"] == (0 if workload_failure else 1)
    assert "profile" in receipt
    assert "raw_profile_samples" in receipt
    if workload_failure:
        assert receipt["samples"][0]["exception_type"] == "ReadError"
    assert "requests_per_second" not in receipt


def test_http_benchmark_external_cancel_before_children_start_records_every_index(monkeypatch):
    samples, calls = [], []
    create_task = asyncio.create_task

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            assert len(samples) == 3, "Early cancellation lost scheduled terminal receipts"

        async def post(self, *_args, **_kwargs):
            calls.append("unexpected HTTP")
            raise AssertionError("Cancelled children must not start HTTP")

    def schedule_then_cancel_parent(coroutine):
        task = create_task(coroutine)
        asyncio.current_task().cancel()
        return task

    monkeypatch.setattr(benchmark.httpx, "AsyncClient", Client)
    monkeypatch.setattr(benchmark.asyncio, "create_task", schedule_then_cancel_parent)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            benchmark.workload("http://unused.invalid", benchmark.facts_for(benchmark.commands(1)), 3, 2, samples)
        )
    assert calls == []
    assert sorted(sample["index"] for sample in samples) == [0, 1, 2]
    assert all(sample["state"] == "cancelled" for sample in samples)
    assert all(sample["elapsed_ms"] is None and sample["exception_type"] == "CancelledError" for sample in samples)
