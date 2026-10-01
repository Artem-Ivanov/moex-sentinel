"""Check that the selector drain benchmark consumes every eligible persisted row."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from develop.benchmarks.benchmark_fact_outbox_selector import BASE_TIME, current_selector, dataset
from develop.benchmarks.fact_outbox_baseline_oracle import select_old_batch
from develop.benchmarks.fact_outbox_drain import measure_drain
from trading_automaton.storage.models import CachedAutomationModel, FactOutboxModel


def test_drain_consumes_eligible_rows_and_leaves_blocked_and_failed(tmp_path):
    results = []
    for selector in ("old", "current"):
        factory = dataset(tmp_path / f"{selector}.sqlite", 100, scenario="bootstrap")
        try:
            results.append(measure_drain(factory, selector=selector, batch_size=2))
        finally:
            factory.kw["bind"].dispose()
    assert [result["delivered_facts"] for result in results] == [97, 97]
    assert [result["remaining_facts"] for result in results] == [3, 3]
    assert results[0]["delivery_order_sha256"] == results[1]["delivery_order_sha256"]
    assert all(result["selection_ms"]["samples"] > 1 for result in results)


def test_fully_blocked_queue_has_zero_drain(tmp_path):
    factory = dataset(tmp_path / "blocked.sqlite", 100, scenario="normal")
    try:
        with factory.begin() as session:
            session.execute(
                update(FactOutboxModel)
                .where(FactOutboxModel.delivery_state == "PENDING")
                .values(next_retry_at=datetime(2030, 1, 1, tzinfo=UTC))
            )
        result = measure_drain(factory, selector="current", batch_size=10)
    finally:
        factory.kw["bind"].dispose()
    assert result["delivered_facts"] == 0
    assert result["remaining_facts"] == 100
    assert result["batches"] == 0
    assert result["selection_ms"]["samples"] == 1
    assert result["selection_ms"]["p50"] > 0


def test_many_bootstrap_dataset_drains_in_the_same_order(tmp_path):
    results = []
    for selector in ("old", "current"):
        factory = dataset(tmp_path / f"many-{selector}.sqlite", 100, scenario="many-bootstrap")
        try:
            with factory() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(CachedAutomationModel)
                        .where(CachedAutomationModel.bootstrap_position_cycle_id.is_not(None))
                    )
                    == 3
                )
            results.append(measure_drain(factory, selector=selector, batch_size=2))
        finally:
            factory.kw["bind"].dispose()
    assert [result["delivered_facts"] for result in results] == [97, 97]
    assert results[0]["delivery_order_sha256"] == results[1]["delivery_order_sha256"]


@pytest.mark.parametrize("scenario", ["normal", "bootstrap", "many-bootstrap"])
def test_second_seed_preserves_batch_and_full_drain_order(tmp_path, scenario):
    seed = 20260928
    results = []
    for selector in ("old", "current"):
        factory = dataset(tmp_path / f"{scenario}-{selector}.sqlite", 100, scenario=scenario, seed=seed)
        try:
            now = BASE_TIME + timedelta(days=2)
            for limit in (0, 1, 2, 100):
                assert current_selector(factory, limit, now=now, deadline_ms=0) == select_old_batch(
                    factory, limit, now=now, deadline_ms=0
                )
            results.append(measure_drain(factory, selector=selector, batch_size=10))
        finally:
            factory.kw["bind"].dispose()
    assert [result["delivered_facts"] for result in results] == [97, 97]
    assert [result["remaining_facts"] for result in results] == [3, 3]
    assert results[0]["delivery_order_sha256"] == results[1]["delivery_order_sha256"]


def test_bootstrap_read_rows_count_separates_old_orm_and_current_metadata(tmp_path):
    results = []
    for selector in ("old", "current"):
        factory = dataset(tmp_path / f"reads-{selector}.sqlite", 100, scenario="many-bootstrap")
        try:
            results.append(measure_drain(factory, selector=selector, batch_size=1000, count_read_rows=True))
        finally:
            factory.kw["bind"].dispose()
    assert [result["bootstrap_read_rows"] for result in results] == [99, 100]
    assert all(result["delivered_facts"] == 97 for result in results)
    assert all(result["row_count_probes_in_wall_ms"] for result in results)
