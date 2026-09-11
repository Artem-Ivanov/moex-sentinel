"""Characterize durable delivery and the benchmark's reported work."""

import asyncio

import pytest

from develop.benchmarks.runtime import run_case


@pytest.mark.parametrize("instruments", [2, 6])
def test_benchmark_recovers_lost_ack_and_measures_actual_decisions(instruments):
    result = asyncio.run(run_case(instruments, warmup=1, iterations=3))
    assert result["instruments"] == instruments
    assert result["measured_decisions"] == instruments * 3
    assert result["measured_reason_counts"] == {"NO_THRESHOLD": instruments * 3}
    assert result["tick_ms"]["samples"] == 3
    assert result["tick_ms"]["min"] <= result["tick_ms"]["p50"] <= result["tick_ms"]["p95"]
    assert result["tick_ms"]["p95"] <= result["tick_ms"]["p99"] <= result["tick_ms"]["max"]
    assert result["decisions_per_second"] > 0
    recovery = result["recovery"]
    assert recovery["backlog_before_restart"] > 0
    assert recovery["backlog_after_restart"] == recovery["backlog_before_restart"]
    assert recovery["backlog_final"] == 0
    assert recovery["replayed_facts"] > 0
    assert recovery["core_decisions"] == recovery["worker_decisions"]
    assert recovery["orders_before_restart"] == recovery["orders_after_restart"] == instruments


@pytest.mark.parametrize(("instruments", "warmup", "iterations"), [(0, 1, 1), (1, -1, 1), (1, 0, 0)])
def test_benchmark_rejects_invalid_workloads(instruments, warmup, iterations):
    with pytest.raises(ValueError, match="must be positive"):
        asyncio.run(run_case(instruments, warmup=warmup, iterations=iterations))


def test_benchmark_ignores_ambient_strategy_settings(monkeypatch):
    monkeypatch.setenv("STRATEGY_ENABLED", "false")
    monkeypatch.setenv("STRATEGY_BUY_ORDER_LOTS", "900000000")
    result = asyncio.run(run_case(2, warmup=0, iterations=1))
    assert result["recovery"]["orders_after_restart"] == 2
