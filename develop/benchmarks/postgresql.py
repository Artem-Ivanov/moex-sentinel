"""Profile Worker SQLite → real loopback HTTP → isolated Core PostgreSQL."""

import argparse
import asyncio
import json
import os
import platform
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from sqlalchemy import func, schema, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from alembic.config import Config
from develop.benchmarks.pg_metrics import Profile, TimedOutboxWriter, instrument_database
from develop.benchmarks.pg_server import CoreServer, NetworkDelivery
from develop.benchmarks.runtime import Harness, require
from develop.benchmarks.synthetic import START, SyntheticBroker, SyntheticMarket, commands
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import AutomationEventModel, BrokerOrderModel
from moex_sentinel.storage.models import TradeDecisionModel as CoreDecisionModel
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from trading_automaton.domain.storage_dtos import TradingCycleState
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.storage.database import create_worker_engine
from trading_automaton.storage.models import Base, FactOutboxModel, TradeDecisionModel
from trading_automaton.storage.repository import LocalAutomationRepository


@contextmanager
def isolated_database(database_url):
    """Migrate and drop only a freshly generated schema on a caller-supplied test DB."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("This benchmark requires a separate PostgreSQL test database")
    admin = create_database_engine(url)
    schema_name = f"profile_{uuid4().hex}"
    created = False
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(schema.CreateSchema(schema_name))
        created = True
        isolated = url.update_query_dict({"options": f"-csearch_path={schema_name}"})
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", isolated.render_as_string(hide_password=False).replace("%", "%%"))
        command.upgrade(config, "head")
        engine = create_database_engine(isolated)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        try:
            if created:
                with admin.begin() as connection:
                    connection.execute(schema.DropSchema(schema_name, cascade=True))
        finally:
            admin.dispose()


class CountedBroker(SyntheticBroker):
    def __init__(self, values, market):
        super().__init__(values, market)
        self.dispatch_calls = 0

    async def dispatch_limit_order(self, request):
        # Count SDK entry BEFORE synthetic assertions: runtime can catch errors.
        self.dispatch_calls += 1
        return await super().dispatch_limit_order(request)


class SeedSession(Session):
    """Seed the existing fixture in parent-before-child order with actual FK checks."""

    def add(self, instance, *, _warn=True):
        super().add(instance, _warn=_warn)
        self.flush()


class PostgreSQLHarness(Harness):
    """Reuse runtime behavior without allocating the older Core SQLite database."""

    def __init__(self, folder, count, engine, batch_size):
        self.values = commands(count)
        self.market = SyntheticMarket(self.values)
        self.broker = CountedBroker(self.values, self.market)
        self.profile = Profile()
        self.batch_size = batch_size
        self.worker_url = f"sqlite:///{folder / 'worker.db'}"
        self.worker_engine = create_worker_engine(self.worker_url)
        self.core_engine = engine
        self.bundle = self.client = self.server = self.delivery = None
        self.restore_metrics = lambda: None

    def initialize(self):
        Base.metadata.create_all(self.worker_engine)
        self.core_factory = sessionmaker(self.core_engine, class_=SeedSession, expire_on_commit=False)
        self.seed_core()
        session_class, self.restore_metrics = instrument_database(self.core_engine, self.profile)
        self.core_factory = sessionmaker(self.core_engine, class_=session_class, expire_on_commit=False)
        ingress = TradingFactIngressService(
            lambda: TradingFactsUnitOfWork(self.core_factory), TradingFactMapper(), now=lambda: self.market.now
        )
        self.server = CoreServer(ingress, self.profile)
        self.server.start()
        self.delivery = NetworkDelivery(self.server.url, self.profile)
        self.open_repository()
        for value in self.values:
            self.repository.cache_command(value)
            self.repository.save_cycle_state(TradingCycleState(str(value.automation_id), 90, None, True, None, START))

    def open_repository(self):
        self.factory = sessionmaker(self.worker_engine, expire_on_commit=False)
        self.repository = LocalAutomationRepository(
            self.factory, fact_writer=TimedOutboxWriter(self.profile, clock=lambda: self.market.now)
        )
        self.synchronizer = FactSynchronizationService(
            self.repository,
            self.delivery,
            now=lambda: self.market.now,
            sleep=lambda _: None,
            retry_limit=0,
            batch_size=self.batch_size,
            deadline_ms=0,
        )

    async def close(self):
        try:
            if self.bundle is not None:
                await self.bundle.close()
        finally:
            try:
                if self.client is not None:
                    await self.client.aclose()
            finally:
                try:
                    try:
                        if self.delivery is not None:
                            self.delivery.close()
                    finally:
                        if self.server is not None:
                            self.server.close()
                finally:
                    self.restore_metrics()
                    self.worker_engine.dispose()

    def outbox_envelopes(self):
        with self.factory() as db:
            rows = db.scalars(select(FactOutboxModel)).all()
            return {
                row.event_id: {
                    key: getattr(row, key)
                    for key in (
                        "event_id",
                        "user_broker_id",
                        "automation_id",
                        "sequence_number",
                        "expected_revision",
                        "fact_kind",
                        "payload",
                        "safe_message",
                        "occurred_at",
                    )
                }
                for row in rows
            }


async def measured_tick(harness):
    harness.profile.add("decision_tick_ms", await harness.tick())
    backlog = harness.count(FactOutboxModel)
    harness.profile.add("outbox_drain_ms", harness.drain())
    return backlog


def workload_report(harness, before, backlogs, elapsed):
    measured = harness.count(TradeDecisionModel) - before
    result = harness.profile.report()
    ticks_ms = result["stage_totals_ms"]["decision_tick_ms"]
    result.update(
        measured_decisions=measured,
        decisions_per_second=measured / (ticks_ms / 1000),
        pipeline_decisions_per_second=measured / elapsed,
        pipeline_facts_per_second=result["delivered_facts"] / elapsed,
        wall_elapsed_seconds=elapsed,
        outbox_backlog_peak=max(backlogs),
    )
    return result


async def recover_lost_ack(harness):
    profile = harness.profile
    profile.reset()
    dispatches_before = harness.broker.dispatch_calls
    core_orders_before = harness.count(BrokerOrderModel, core=True)
    await harness.tick()
    original = harness.outbox_envelopes()
    before_core = harness.count(AutomationEventModel, core=True)
    harness.server.app.armed = True
    require(not harness.synchronizer.flush_outbox(), "Expected truncated committed HTTP acknowledgement")
    committed = harness.count(AutomationEventModel, core=True) - before_core
    require(committed == len(harness.delivery.replay_expected) > 0, "Lost ACK must occur after actual commit")
    committed_snapshot = core_envelopes(harness)
    # Unacknowledged rows gate decisions. Advance logical retry eligibility too.
    await harness.tick()
    before_restart = harness.outbox_envelopes()
    require(original == before_restart, "Pending immutable outbox changed before restart")
    started = perf_counter()
    await harness.restart()
    after_restart = harness.outbox_envelopes()
    require(before_restart == after_restart, "Restart changed persisted immutable outbox")
    harness.drain()
    require(not harness.delivery.replay_expected, "Committed facts were not replayed")
    after_core = core_envelopes(harness)
    require(
        all(after_core.get(event_id) == envelope for event_id, envelope in committed_snapshot.items()),
        "Replay changed committed immutable envelopes",
    )
    require(len(after_core) == before_core + len(original), "Replay lost or duplicated envelopes")
    elapsed = (perf_counter() - started) * 1000
    await harness.tick()
    harness.drain()
    worker_count = harness.count(TradeDecisionModel)
    core_count = harness.count(CoreDecisionModel, core=True)
    core_orders_after = harness.count(BrokerOrderModel, core=True)
    require(worker_count == core_count, "Worker/Core decision counts differ")
    require(harness.broker.dispatch_calls == dispatches_before, "Recovery re-entered broker SDK dispatch")
    require(core_orders_before == core_orders_after, "Recovery duplicated Core orders")
    return {
        "backlog_before_restart": len(before_restart),
        "backlog_after_restart": len(after_restart),
        "backlog_final": harness.count(FactOutboxModel),
        "committed_before_restart": committed,
        "replayed_facts": harness.delivery.replayed_facts,
        "lost_ack_transport_errors": harness.delivery.transport_errors,
        "restart_and_drain_ms": elapsed,
        "worker_decisions": worker_count,
        "core_decisions": core_count,
        "sdk_dispatches_before": dispatches_before,
        "sdk_dispatches_after": harness.broker.dispatch_calls,
        "core_orders_before": core_orders_before,
        "core_orders_after": core_orders_after,
        "immutable_envelopes_preserved": True,
    }


def core_envelopes(harness):
    with harness.core_factory() as db:
        return {
            row.event_id: tuple(getattr(row, column.name) for column in AutomationEventModel.__table__.columns)
            for row in db.scalars(select(AutomationEventModel)).all()
        }


async def run_case(database_url, instruments, *, warmup=3, iterations=100, batch_size=100):
    if instruments < 1 or warmup < 0 or iterations < 1 or batch_size < 1:
        raise ValueError("instruments/iterations/batch_size must be positive and warmup nonnegative")
    with isolated_database(database_url) as engine, TemporaryDirectory(prefix="sentinel-pg-profile-") as folder:
        harness = PostgreSQLHarness(Path(folder), instruments, engine, batch_size)
        try:
            harness.initialize()
            await harness.start()
            before = harness.count(TradeDecisionModel)
            started = perf_counter()
            backlogs = []
            for _ in range(instruments + 2):
                backlogs.append(await measured_tick(harness))
                if len(harness.broker.orders) == instruments:
                    break
            initial = workload_report(harness, before, backlogs, perf_counter() - started)
            require(harness.broker.dispatch_calls == instruments, "Initial fills did not complete exactly once")
            require(all(quantity == 1 for quantity in harness.broker.quantities.values()), "Initial positions differ")
            initial["broker_fills"] = len(harness.broker.orders)
            for _ in range(warmup):
                await harness.tick()
                harness.drain()
            harness.profile.reset()
            before = harness.count(TradeDecisionModel)
            measured_after = harness.market.now
            backlogs = []
            started = perf_counter()
            for _ in range(iterations):
                backlogs.append(await measured_tick(harness))
            wait = workload_report(harness, before, backlogs, perf_counter() - started)
            with harness.factory() as db:
                wait["reason_counts"] = dict(
                    db.execute(
                        select(TradeDecisionModel.reason_code, func.count())
                        .where(TradeDecisionModel.occurred_at > measured_after)
                        .group_by(TradeDecisionModel.reason_code)
                    ).all()
                )
            require(wait["measured_decisions"] == instruments * iterations, "Missing measured WAIT decisions")
            require(wait["reason_counts"] == {"NO_THRESHOLD": instruments * iterations}, "Non-WAIT measured workload")
            require(harness.broker.dispatch_calls == instruments, "Measured WAIT entered broker SDK")
            recovery = await recover_lost_ack(harness)
            return {
                "instruments": instruments,
                "postgresql_version": ".".join(str(part) for part in engine.dialect.server_version_info),
                "warmup": warmup,
                "iterations": iterations,
                "batch_size": batch_size,
                "deadline_ms": 0,
                "retry_limit": 0,
                "initial_buy_filled": initial,
                "wait": wait,
                "recovery": recovery,
            }
        finally:
            await harness.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", nargs="+", type=int, default=[6, 20, 50])
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    database_url = os.environ.get("PG_PROFILE_DATABASE_URL")
    if not database_url:
        parser.error("PG_PROFILE_DATABASE_URL must point to a disposable PostgreSQL test database")
    results = [
        asyncio.run(
            run_case(database_url, count, warmup=args.warmup, iterations=args.iterations, batch_size=args.batch_size)
        )
        for count in args.instruments
    ]
    sys.stdout.write(
        json.dumps(
            {
                "schema_version": 1,
                "python": platform.python_version(),
                "platform": platform.system(),
                "database_storage": os.environ.get("PG_PROFILE_STORAGE_KIND", "unspecified external test storage"),
                "workload": (
                    "synthetic broker/market; Analytics ASGI; Worker SQLite; CoreClient TCP HTTP; Core PostgreSQL"
                ),
                "clock": "logical minute per tick for domain; all latency uses perf_counter monotonic",
                "queue_clock": (
                    "real outbox ORM creation to first CoreClient HTTP attempt; includes Worker transaction wait"
                ),
                "percentiles": "p50 median; p95/p99 nearest rank; no prespecified SLA threshold",
                "overlap": (
                    "HTTP includes Core ingress; ingress includes SQL and session commit; "
                    "session commit includes flush SQL and DBAPI commit. Do not sum these quantiles."
                ),
                "conditions": (
                    "serial decision then explicit drain; warmup excluded from WAIT; "
                    "initial BUY/FILLED includes intervening WAIT/rate limiter; "
                    "one isolated Alembic-head schema per case"
                ),
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
