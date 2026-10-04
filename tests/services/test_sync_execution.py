"""Neutral runner compatibility and mixed database/async SDK thread ownership."""

import asyncio
import threading

import pytest

from moex_sentinel.api.sync_execution import SyncExecutor
from moex_sentinel.services.connections import BrokerConnectionService
from moex_sentinel.services.instrument_catalog import InstrumentCatalogService
from moex_sentinel.services.market_snapshot_gateway import MarketSnapshotGateway
from moex_sentinel.services.position_adoption import PositionAdoptionService
from moex_sentinel.services.sync_execution import bind_sync_runner, run_sync
from sentinel_contracts.analytics import MarketSnapshotRequest
from tests.services.test_connection_service import broker
from tests.services.test_instrument_catalog_service import Adapter, Brokers, Catalog, Environment
from tests.services.test_market_snapshot_gateway import SOURCE, Source, settle
from tests.services.test_position_adoption_service import Broker, Repository, instrument, position


def test_default_inline_explicit_thread_fallback_and_context_reset_after_failure_and_cancellation():
    async def scenario():
        loop_thread = threading.get_ident()
        assert await run_sync(threading.get_ident) == loop_thread
        assert await run_sync(threading.get_ident, fallback=asyncio.to_thread) != loop_thread
        executor = SyncExecutor(capacity=1)

        def failure():
            raise ValueError("synthetic reset")

        with bind_sync_runner(executor.run):
            assert await run_sync(threading.get_ident) != loop_thread
            with pytest.raises(ValueError, match="synthetic"):
                await run_sync(failure)
        assert await run_sync(threading.get_ident) == loop_thread
        entered = asyncio.Event()

        async def child():
            with bind_sync_runner(executor.run):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(child())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await run_sync(threading.get_ident) == loop_thread
        with bind_sync_runner(executor.run):
            inherited = asyncio.create_task(run_sync(threading.get_ident))
        assert await inherited != loop_thread
        await executor.aclose()
        with bind_sync_runner(executor.run), pytest.raises(RuntimeError, match="closed"):
            await run_sync(lambda: "late inherited context")

    asyncio.run(scenario())


def test_connection_reads_in_worker_but_adapter_and_sdk_remain_in_loop():
    async def scenario():
        loop_thread = threading.get_ident()
        reads, sdk = [], []
        record = broker()

        class Brokers:
            def get(self, _broker_id):
                reads.append(threading.get_ident())
                return record

        class Adapter:
            async def list_accounts(self):
                sdk.append(threading.get_ident())
                return ()

        def adapter_factory(_broker):
            sdk.append(threading.get_ident())
            return Adapter()

        executor = SyncExecutor(capacity=1)
        try:
            with bind_sync_runner(executor.run):
                assert (await BrokerConnectionService(Brokers(), adapter_factory).check(record.id)).available
            assert reads
            assert all(value != loop_thread for value in reads)
            assert sdk == [loop_thread, loop_thread]
        finally:
            await executor.aclose()

    asyncio.run(scenario())


def test_adoption_database_after_sdk_keeps_broker_calls_in_loop():
    async def scenario():
        loop_thread = threading.get_ident()
        database, sdk = [], []

        class ObservedRepository(Repository):
            def find_instrument(self, *args):
                database.append(threading.get_ident())
                return super().find_instrument(*args)

            def adopt(self, candidate):
                database.append(threading.get_ident())
                return super().adopt(candidate)

        class ObservedBroker(Broker):
            async def get_positions(self, account_id):
                sdk.append(threading.get_ident())
                return await super().get_positions(account_id)

            async def list_active_orders(self, *args):
                sdk.append(threading.get_ident())
                return await super().list_active_orders(*args)

        executor = SyncExecutor(capacity=1)
        try:
            with bind_sync_runner(executor.run):
                result = await PositionAdoptionService(ObservedRepository(instrument())).adopt(
                    "00000000-0000-4000-8000-000000000101", "account-1", ObservedBroker((position(),))
                )
            assert result.adopted == 1
            assert len(database) == 2
            assert all(value != loop_thread for value in database)
            assert sdk == [loop_thread, loop_thread]
        finally:
            await executor.aclose()

    asyncio.run(scenario())


def test_catalog_preflight_and_post_sdk_reconciliation_and_state_reads_use_worker():

    async def scenario():
        loop_thread = threading.get_ident()
        database, sdk = [], []

        class ObservedBrokers(Brokers):
            def get(self, broker_id):
                database.append(("broker", threading.get_ident()))
                return super().get(broker_id)

        class ObservedCatalog(Catalog):
            def reconcile(self, *args):
                database.append(("reconcile", threading.get_ident()))
                return super().reconcile(*args)

            def get(self, *args):
                database.append(("instrument", threading.get_ident()))
                return super().get(*args)

            def sync_state(self, *args):
                database.append(("sync_state", threading.get_ident()))
                return super().sync_state(*args)

        class ObservedAdapter(Adapter):
            async def list_instruments(self):
                sdk.append(threading.get_ident())
                return await super().list_instruments()

            async def get_last_prices(self, *args):
                sdk.append(threading.get_ident())
                return await super().get_last_prices(*args)

        def factory(_broker):
            sdk.append(threading.get_ident())
            return ObservedAdapter()

        executor = SyncExecutor(capacity=1)
        try:
            with bind_sync_runner(executor.run):
                service = InstrumentCatalogService(ObservedBrokers(), ObservedCatalog(), factory, Environment())
                assert (await service.synchronize("broker-1")).added == 2
                assert (await service.details("broker-1", "share")).instrument.instrument_id == "share"
            assert [name for name, _thread in database] == ["broker", "reconcile", "broker", "instrument", "sync_state"]
            assert all(thread != loop_thread for _name, thread in database)
            assert sdk == [loop_thread] * 4
        finally:
            await executor.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("bound", [False, True])
def test_gateway_thread_fallback_and_bound_pool_keep_source_runtime_on_loop(bound):

    async def scenario():
        loop_thread = threading.get_ident()
        sync_calls, runtime_calls = [], []

        class ObservedSource(Source):
            async def start(self):
                runtime_calls.append(threading.get_ident())
                return await super().start()

            async def close(self):
                runtime_calls.append(threading.get_ident())
                return await super().close()

        source = ObservedSource()

        def resolver(source_id):
            sync_calls.append(threading.get_ident())
            return source_id

        def factory(_configuration):
            sync_calls.append(threading.get_ident())
            return source

        gateway = MarketSnapshotGateway(factory, configuration_resolver=resolver)
        executor = SyncExecutor(capacity=1)
        try:
            if bound:
                with bind_sync_runner(executor.run):
                    await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",)))
            else:
                await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",)))
            await settle()
            await gateway.close()
            assert len(sync_calls) == 2
            assert all(thread != loop_thread for thread in sync_calls)
            assert runtime_calls == [loop_thread, loop_thread]
        finally:
            await gateway.close()
            await executor.aclose()

    asyncio.run(scenario())
