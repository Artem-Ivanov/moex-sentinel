"""Measure real in-process Analytics/Worker/Core with disposable SQLite files."""

import argparse
import asyncio
import json
import math
import platform
import statistics
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from develop.benchmarks.synthetic import START, SyntheticBroker, SyntheticMarket, commands
from market_analytics.app import create_app
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.storage.models import Base as CoreBase
from moex_sentinel.storage.models import BrokerInstrumentModel, TradingAutomationModel, UserBrokerModel
from moex_sentinel.storage.models import TradeDecisionModel as CoreDecisionModel
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from sentinel_contracts.broker_execution import BrokerConnection
from trading_automaton.adapters.analytics_client import AnalyticsClient
from trading_automaton.composition import build_broker_runtime
from trading_automaton.config import StrategySettings
from trading_automaton.domain.storage_dtos import TradingCycleState
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.fact_outbox import FactOutboxWriter
from trading_automaton.storage.models import Base, FactOutboxModel, TradeDecisionModel
from trading_automaton.storage.repository import LocalAutomationRepository


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def distribution(samples):
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "min": ordered[0],
        "p50": statistics.median(ordered),
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "p99": ordered[math.ceil(len(ordered) * 0.99) - 1],
        "max": ordered[-1],
    }


def strategy_settings():
    return StrategySettings(
        STRATEGY_BUY_ORDER_LOTS=1,
        STRATEGY_STOP_LOSS_PERCENT="5",
        STRATEGY_TAKE_PROFIT_PERCENT="6",
        STRATEGY_AVERAGING_STEP_PERCENT="0.5",
        STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT="0.5",
        STRATEGY_PARTIAL_SELL_PERCENT="25",
        STRATEGY_MAX_PARTIAL_SELL_STEPS=3,
        STRATEGY_ORDER_TTL_SECONDS=10,
        STRATEGY_ORDER_RETRY_LIMIT=3,
        STRATEGY_CORE_RETRY_LIMIT=5,
        STRATEGY_ENABLED=True,
    )


class CoreDelivery:
    """Inject an ambiguous delivery outcome after the real Core transaction."""

    def __init__(self, ingress):
        self.ingress = ingress
        self.lose_ack = False
        self.pending_replay = {}
        self.replayed_facts = 0

    def publish_facts(self, facts):
        result = self.ingress.publish(facts)
        require(not result.failures, "Core rejected synthetic facts")
        if self.lose_ack:
            self.pending_replay = {fact.event_id: fact.model_dump(mode="json") for fact in facts}
            raise httpx.ReadTimeout("Synthetic response loss after commit")
        for fact in facts:
            original = self.pending_replay.pop(fact.event_id, None)
            if original is not None:
                require(original == fact.model_dump(mode="json"), "Retry changed immutable fact")
                self.replayed_facts += 1
        return result


class Harness:
    def __init__(self, folder, count):
        self.values = commands(count)
        self.market = SyntheticMarket(self.values)
        self.broker = SyntheticBroker(self.values, self.market)
        self.worker_url = f"sqlite:///{folder / 'worker.db'}"
        self.worker_engine = create_worker_engine(self.worker_url)
        self.core_engine = create_engine(f"sqlite:///{folder / 'core.db'}")
        self.bundle = None
        self.client = None
        Base.metadata.create_all(self.worker_engine)
        CoreBase.metadata.create_all(self.core_engine)
        self.core_factory = sessionmaker(self.core_engine, expire_on_commit=False)
        self.seed_core()
        ingress = TradingFactIngressService(
            lambda: TradingFactsUnitOfWork(self.core_factory), TradingFactMapper(), now=lambda: self.market.now
        )
        self.delivery = CoreDelivery(ingress)
        self.open_repository()
        for value in self.values:
            self.repository.cache_command(value)
            self.repository.save_cycle_state(TradingCycleState(str(value.automation_id), 90, None, True, None, START))

    def seed_core(self):
        first = self.values[0]
        with self.core_factory.begin() as db:
            db.add(
                UserBrokerModel(
                    id=str(first.user_broker_id),
                    api_slug="t_invest",
                    display_name="Synthetic benchmark",
                    environment="TEST",
                    fqdn="unused.invalid",
                    settings={},
                    external_account_id=first.account_id,
                    state="ACTIVE",
                    created_at=START,
                    updated_at=START,
                )
            )
            for value in self.values:
                db.add(
                    BrokerInstrumentModel(
                        id=str(value.instrument_id),
                        user_broker_id=str(value.user_broker_id),
                        external_instrument_id=value.external_instrument_id,
                        external_identifiers={},
                        ticker=value.external_instrument_id,
                        name="Synthetic",
                        instrument_type="SHARE",
                        class_code="TQBR",
                        currency="RUB",
                        lot_size=10,
                        min_price_increment=value.min_price_increment,
                        api_trade_available=True,
                        is_active=True,
                        is_selected=True,
                        first_seen_at=START,
                        last_seen_at=START,
                        created_at=START,
                        updated_at=START,
                    )
                )
                db.add(
                    TradingAutomationModel(
                        id=str(value.automation_id),
                        user_broker_id=str(value.user_broker_id),
                        instrument_id=str(value.instrument_id),
                        state="IN_WORK",
                        revision=1,
                        last_sequence_number=0,
                        resume_requested=False,
                        created_at=START,
                        updated_at=START,
                    )
                )

    def open_repository(self):
        self.factory = sessionmaker(self.worker_engine, expire_on_commit=False)
        self.repository = LocalAutomationRepository(
            self.factory, fact_writer=FactOutboxWriter(clock=lambda: self.market.now)
        )
        self.synchronizer = FactSynchronizationService(
            self.repository,
            self.delivery,
            now=lambda: self.market.now,
            sleep=lambda _: None,
            retry_limit=0,
            batch_size=100,
            deadline_ms=0,
        )

    async def start(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(self.market, now=lambda: self.market.now)),
            base_url="http://analytics.invalid",
        )
        self.bundle = await build_broker_runtime(
            BrokerConnection(str(self.values[0].broker_id), "TINVEST_SANDBOX", "unused.invalid", "synthetic", True),
            self.repository,
            strategy_settings=strategy_settings(),
            session_factory=lambda _: self.broker,
            now=lambda: self.market.now,
            analytics_client=AnalyticsClient(self.client),
        )
        await self.bundle.runtime.replace_commands(self.values)

    async def tick(self):
        self.market.advance()
        started = perf_counter()
        await self.bundle.runtime.run_once()
        await self.bundle.tracking.wait_all()
        return (perf_counter() - started) * 1000

    def count(self, model, *, core=False):
        with (self.core_factory if core else self.factory)() as db:
            return db.scalar(select(func.count()).select_from(model))

    def drain(self):
        started = perf_counter()
        while (previous := self.count(FactOutboxModel)) > 0:
            require(self.synchronizer.flush_outbox(), "Unexpected fact delivery outage")
            require(self.count(FactOutboxModel) < previous, "Outbox stopped making progress")
        return (perf_counter() - started) * 1000

    async def restart(self):
        await self.bundle.close()
        self.bundle = None
        await self.client.aclose()
        self.worker_engine.dispose()
        self.worker_engine = create_worker_engine(self.worker_url)
        self.open_repository()
        await self.start()

    async def close(self):
        try:
            if self.bundle is not None:
                await self.bundle.close()
        finally:
            if self.client is not None:
                await self.client.aclose()
            self.worker_engine.dispose()
            self.core_engine.dispose()


async def run_case(instruments, *, warmup=3, iterations=200):
    if instruments < 1 or warmup < 0 or iterations < 1:
        raise ValueError("instruments/iterations must be positive and warmup nonnegative")
    with TemporaryDirectory(prefix="sentinel-runtime-benchmark-") as temporary:
        harness = Harness(Path(temporary), instruments)
        try:
            await harness.start()
            # The real broker rate limiter allows two new orders per tick. Establish
            # all positions before timing the steady constant-market WAIT workload.
            for _ in range(instruments + 2):
                await harness.tick()
                harness.drain()
                if len(harness.broker.orders) == instruments:
                    break
            require(len(harness.broker.orders) == instruments, "Initial fills did not complete")
            require(
                all(quantity == 1 for quantity in harness.broker.quantities.values()),
                "Initial workload must hold one lot per instrument",
            )
            for _ in range(warmup):
                await harness.tick()
                harness.drain()
            before = harness.count(TradeDecisionModel)
            measured_after = harness.market.now
            orders_before = len(harness.broker.orders)
            ticks, delivery, backlogs = [], [], []
            for _ in range(iterations):
                ticks.append(await harness.tick())
                backlogs.append(harness.count(FactOutboxModel))
                delivery.append(harness.drain())
            measured = harness.count(TradeDecisionModel) - before
            require(measured == instruments * iterations, "Missing measured decisions")
            require(len(harness.broker.orders) == orders_before, "Measured workload submitted orders instead of WAIT")
            with harness.factory() as db:
                measured_reasons = dict(
                    db.execute(
                        select(TradeDecisionModel.reason_code, func.count())
                        .where(TradeDecisionModel.occurred_at > measured_after)
                        .group_by(TradeDecisionModel.reason_code)
                    ).all()
                )
            require(measured_reasons == {"NO_THRESHOLD": measured}, "Measured workload deviated from strategy WAIT")
            recovery = await recover_lost_ack(harness)
            return {
                "instruments": instruments,
                "warmup": warmup,
                "iterations": iterations,
                "measured_decisions": measured,
                "measured_reason_counts": measured_reasons,
                "tick_ms": distribution(ticks),
                "delivery_ms": distribution(delivery),
                "decisions_per_second": measured / (sum(ticks) / 1000),
                "pipeline_decisions_per_second": measured / ((sum(ticks) + sum(delivery)) / 1000),
                "outbox_backlog_peak": max(backlogs),
                "recovery": recovery,
            }
        finally:
            await harness.close()


async def recover_lost_ack(harness):
    orders_before = len(harness.broker.orders)
    await harness.tick()
    harness.delivery.lose_ack = True
    require(not harness.synchronizer.flush_outbox(), "Expected lost acknowledgement")
    # Exercise another tick while committed facts still await acknowledgement.
    # Hydration blocks fresh decisions for automations with pending outbox rows.
    await harness.tick()
    before_restart = harness.count(FactOutboxModel)
    started = perf_counter()
    await harness.restart()
    after_restart = harness.count(FactOutboxModel)
    require(before_restart == after_restart, "Restart lost queued facts")
    harness.delivery.lose_ack = False
    harness.drain()
    require(not harness.delivery.pending_replay, "Committed facts were not replayed")
    elapsed = (perf_counter() - started) * 1000
    await harness.tick()
    harness.drain()
    worker_count = harness.count(TradeDecisionModel)
    core_count = harness.count(CoreDecisionModel, core=True)
    require(worker_count == core_count, "Core and Worker decision counts differ")
    require(len(harness.broker.orders) == orders_before, "Restart submitted another order")
    return {
        "backlog_before_restart": before_restart,
        "backlog_after_restart": after_restart,
        "backlog_final": harness.count(FactOutboxModel),
        "replayed_facts": harness.delivery.replayed_facts,
        "restart_and_drain_ms": elapsed,
        "worker_decisions": worker_count,
        "core_decisions": core_count,
        "orders_before_restart": orders_before,
        "orders_after_restart": len(harness.broker.orders),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", nargs="+", type=int, default=[6, 20, 50])
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=200)
    args = parser.parse_args()
    results = [
        asyncio.run(run_case(count, warmup=args.warmup, iterations=args.iterations)) for count in args.instruments
    ]
    sys.stdout.write(
        json.dumps(
            {
                "schema_version": 1,
                "python": platform.python_version(),
                "platform": platform.system(),
                "workload": "initial BUY/FILLED, measured WAIT; in-process ASGI; Core and Worker temporary SQLite",
                "clock": "deterministic logical minute per tick; latency uses perf_counter",
                "percentiles": "p50 median; p95/p99 nearest rank; no SLA threshold",
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
