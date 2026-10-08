"""Market gateway owns streams and never fabricates fresh market observations."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from moex_sentinel.services.market_recovery import MarketRecoveryPolicy
from moex_sentinel.services.market_snapshot_gateway import LOGGER, MarketSnapshotGateway
from sentinel_contracts.analytics import MarketSnapshotRequest
from sentinel_contracts.broker_errors import BrokerOperationError
from sentinel_contracts.broker_execution import OrderBookLevel
from sentinel_contracts.streaming_market import StreamCandle, StreamOrderBook, StreamTradingStatus

NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)
SOURCE = UUID("00000000-0000-0000-0000-000000000001")


def candle(at, close="10"):
    return StreamCandle("AAA", Decimal("10"), Decimal("11"), Decimal("9"), Decimal(close), 1, at, True, at)


class Source:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.subscriptions = set()
        self.history_gate = asyncio.Event()
        self.history_gate.set()
        self.history_calls = []
        self.starts = 0
        self.closes = 0

    async def start(self):
        self.starts += 1

    async def close(self):
        self.closes += 1

    async def replace_subscriptions(self, instruments):
        self.subscriptions = set(instruments)

    async def get_candles(self, instrument_id, start, end):
        self.history_calls.append(instrument_id)
        await self.history_gate.wait()
        return (candle(NOW - timedelta(minutes=2)),) if instrument_id == "AAA" else ()

    async def events(self):
        while True:
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            yield item


async def settle():
    for _ in range(30):
        await asyncio.sleep(0)


async def pump_until(predicate):
    """Advance cooperative tasks without assuming a wall-clock scheduling budget."""
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("Expected source acknowledgement did not arrive")


async def ready(source):
    await source.queue.put(
        StreamOrderBook("AAA", (OrderBookLevel(Decimal("10"), 2),), (OrderBookLevel(Decimal("11"), 2),), NOW, True)
    )
    await source.queue.put(StreamTradingStatus("AAA", "NORMAL_TRADING", True, True, NOW))
    await settle()


def test_lazy_union_and_stable_snapshot_preserve_capture_time():
    async def run():
        source = Source()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: NOW)
        assert source.starts == 0
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        initial = await gateway.snapshot(request)
        assert not initial.instruments[0].available
        await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("BBB",)))
        await ready(source)
        snapshot = await gateway.snapshot(request)
        again = await gateway.snapshot(request)
        assert source.subscriptions == {"AAA", "BBB"}
        assert source.starts == 1
        assert snapshot.instruments[0].available
        assert snapshot.snapshot_id == again.snapshot_id
        assert snapshot.captured_at == NOW
        assert snapshot.ttl_ms == 2000
        assert "account_id" not in snapshot.model_dump_json()
        assert "token" not in snapshot.model_dump_json()
        await gateway.close()
        assert source.closes == 1

    asyncio.run(run())


def test_singleflight_bootstrap_merges_stream_arriving_during_history():
    async def run():
        source = Source()
        source.history_gate.clear()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: NOW)
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        await asyncio.gather(*(gateway.snapshot(request) for _ in range(5)))
        await ready(source)
        await source.queue.put(candle(NOW - timedelta(minutes=1), "11"))
        await settle()
        source.history_gate.set()
        await settle()
        snapshot = await gateway.snapshot(request)
        assert source.history_calls == ["AAA"]
        assert [item.close for item in snapshot.instruments[0].candles] == [Decimal("10"), Decimal("11")]
        assert snapshot.instruments[0].available
        await gateway.close()

    asyncio.run(run())


def test_disconnect_invalidates_and_reconnect_requires_new_status():
    async def run():
        source = Source()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: NOW, retry_seconds=0.001)
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        await gateway.snapshot(request)
        await ready(source)
        assert (await gateway.snapshot(request)).instruments[0].available
        await source.queue.put(RuntimeError("synthetic disconnect"))
        await settle()
        assert not (await gateway.snapshot(request)).instruments[0].available
        await asyncio.sleep(0.01)
        await source.queue.put(
            StreamOrderBook("AAA", (OrderBookLevel(Decimal("10"), 2),), (OrderBookLevel(Decimal("11"), 2),), NOW, True)
        )
        await settle()
        assert not (await gateway.snapshot(request)).instruments[0].available
        await ready(source)
        assert (await gateway.snapshot(request)).instruments[0].available
        assert source.starts == 2
        assert source.history_calls == ["AAA", "AAA"]
        await gateway.close()

    asyncio.run(run())


def test_mixed_batch_keeps_each_quote_age_and_does_not_renew_envelope_on_read():
    async def run():
        current = [NOW]
        source = Source()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: current[0])
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA", "BBB"))
        await gateway.snapshot(request)
        await ready(source)
        current[0] = NOW + timedelta(seconds=10)
        await source.queue.put(
            StreamOrderBook(
                "BBB", (OrderBookLevel(Decimal("10"), 2),), (OrderBookLevel(Decimal("11"), 2),), current[0], True
            )
        )
        await source.queue.put(StreamTradingStatus("BBB", "NORMAL_TRADING", True, True, current[0]))
        await settle()
        snapshot = await gateway.snapshot(request)
        assert snapshot.captured_at == NOW + timedelta(seconds=10)
        assert snapshot.instruments[0].market.order_book.captured_at == NOW
        assert snapshot.instruments[1].market.order_book.captured_at == NOW + timedelta(seconds=10)
        current[0] += timedelta(seconds=30)
        again = await gateway.snapshot(request)
        assert again.captured_at == snapshot.captured_at
        assert again.snapshot_id == snapshot.snapshot_id
        await gateway.close()

    asyncio.run(run())


def test_history_backfill_refreshes_without_restarting_connection():
    async def run():
        current = [NOW]
        source = Source()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: current[0], refresh_seconds=0.001)
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        await gateway.snapshot(request)
        await ready(source)
        initial = await gateway.snapshot(request)
        current[0] += timedelta(minutes=1)
        await asyncio.sleep(0.01)
        refreshed = await gateway.snapshot(request)
        assert len(source.history_calls) >= 2
        assert source.starts == 1
        assert refreshed.snapshot_id == initial.snapshot_id
        assert refreshed.captured_at == initial.captured_at
        await gateway.close()

    asyncio.run(run())


def test_revocation_closes_existing_source_and_refuses_cached_market():
    async def run():
        source = Source()
        enabled = [True]

        def resolve(_):
            if not enabled[0]:
                raise ValueError("Source disabled")
            return "configuration-one"

        gateway = MarketSnapshotGateway(lambda _: source, configuration_resolver=resolve, now=lambda: NOW)
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        await gateway.snapshot(request)
        await ready(source)
        assert (await gateway.snapshot(request)).instruments[0].available
        enabled[0] = False
        try:
            await gateway.snapshot(request)
        except ValueError:
            pass
        else:
            raise AssertionError("Revoked source returned cached market data")
        assert source.closes == 1
        await gateway.close()
        assert source.closes == 1

    asyncio.run(run())


def test_rotation_replaces_connection_and_requires_fresh_market_confirmation():
    async def run():
        sources = {"configuration-one": Source(), "configuration-two": Source()}
        revision = ["configuration-one"]
        gateway = MarketSnapshotGateway(
            lambda configuration: sources[configuration],
            configuration_resolver=lambda _: revision[0],
            now=lambda: NOW,
        )
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        await gateway.snapshot(request)
        await ready(sources["configuration-one"])
        before = await gateway.snapshot(request)
        assert before.instruments[0].available
        revision[0] = "configuration-two"
        after = await gateway.snapshot(request)
        assert not after.instruments[0].available
        assert after.snapshot_id != before.snapshot_id
        assert sources["configuration-one"].closes == 1
        await ready(sources["configuration-two"])
        assert (await gateway.snapshot(request)).instruments[0].available
        assert sources["configuration-two"].starts == 1
        await gateway.close()
        assert sources["configuration-two"].closes == 1

    asyncio.run(run())


def test_failed_history_instrument_does_not_restart_healthy_peer_stream():
    class PartlyUnavailableSource(Source):
        async def get_candles(self, instrument_id, start, end):
            if instrument_id == "AAA":
                raise ValueError("Synthetic unavailable instrument")
            return await super().get_candles(instrument_id, start, end)

    async def run():
        source = PartlyUnavailableSource()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: NOW)
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA", "BBB"))
        await gateway.snapshot(request)
        await ready(source)
        await source.queue.put(
            StreamOrderBook("BBB", (OrderBookLevel(Decimal("10"), 2),), (OrderBookLevel(Decimal("11"), 2),), NOW, True)
        )
        await source.queue.put(StreamTradingStatus("BBB", "NORMAL_TRADING", True, True, NOW))
        await settle()
        snapshot = await gateway.snapshot(request)
        assert not snapshot.instruments[0].available
        assert snapshot.instruments[1].available
        assert source.starts == 1
        assert source.closes == 0
        await gateway.close()

    asyncio.run(run())


def test_failed_backfill_keeps_fresh_quotes_after_successful_bootstrap():
    class FailedRefreshSource(Source):
        async def get_candles(self, instrument_id, start, end):
            if self.history_calls:
                self.history_calls.append(instrument_id)
                raise ValueError("Synthetic history refresh failure")
            return await super().get_candles(instrument_id, start, end)

    async def run():
        clock = [NOW]
        source = FailedRefreshSource()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: clock[0], refresh_seconds=0.001)
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        await gateway.snapshot(request)
        await ready(source)
        assert (await gateway.snapshot(request)).instruments[0].available
        clock[0] += timedelta(minutes=1)
        async with asyncio.timeout(1):
            while len(source.history_calls) < 2:  # noqa: ASYNC110 - bounded observation of the public source
                await asyncio.sleep(0.001)
        assert (await gateway.snapshot(request)).instruments[0].available
        assert source.starts == 1
        await gateway.close()

    asyncio.run(run())


def test_cleanup_failure_during_disconnect_does_not_kill_reconnect():
    class FailedCloseSource(Source):
        async def close(self):
            await super().close()
            if self.closes == 1:
                raise RuntimeError("Synthetic first cleanup failure")

    async def run():
        source = FailedCloseSource()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: NOW, retry_seconds=0.001)
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        await gateway.snapshot(request)
        await ready(source)
        await source.queue.put(RuntimeError("Synthetic disconnect"))
        try:
            async with asyncio.timeout(0.1):
                while source.starts < 2:  # noqa: ASYNC110 - bounded observation of the public source
                    await asyncio.sleep(0.001)
            await ready(source)
            assert (await gateway.snapshot(request)).instruments[0].available
        finally:
            await gateway.close()

    asyncio.run(run())


def test_disconnect_diagnostics_include_only_exception_type_and_status(caplog, monkeypatch):
    monkeypatch.setattr(LOGGER, "disabled", False)

    async def run():
        source = Source()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: NOW)
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        await gateway.snapshot(request)
        await source.queue.put(BrokerOperationError("BROKER_INVALID_REQUEST", "MUST_NOT_LOG_DETAILS", retryable=False))
        await settle()
        await gateway.close()

    asyncio.run(run())
    record = next(
        item for item in caplog.records if item.message == "Market source blocked until configuration changes"
    )
    assert record.data == {"exception_types": ["BrokerOperationError"], "error_codes": ["BROKER_INVALID_REQUEST"]}
    assert "MUST_NOT_LOG" not in caplog.text
    assert "MUST_NOT_LOG" not in str(record.data)


@pytest.mark.parametrize("failure", ["code_property", "code_call", "status_property", "unhashable_status"])
def test_diagnostic_accessors_cannot_kill_source_reconnect(failure, caplog, monkeypatch):
    monkeypatch.setattr(LOGGER, "disabled", False)

    class BrokenStatus:
        @property
        def name(self):
            if failure == "status_property":
                raise ValueError("MUST_NOT_LOG_STATUS_DETAILS")
            return []

    class DiagnosticError(RuntimeError):
        @property
        def code(self):
            if failure == "code_property":
                raise ValueError("MUST_NOT_LOG_CODE_DETAILS")
            return self.read_code

        def read_code(self):
            if failure == "code_call":
                raise ValueError("MUST_NOT_LOG_CALL_DETAILS")
            return BrokenStatus()

    async def run():
        source = Source()
        gateway = MarketSnapshotGateway(lambda _: source, now=lambda: NOW, retry_seconds=0.001)
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        await gateway.snapshot(request)
        await ready(source)
        await source.queue.put(DiagnosticError("MUST_NOT_LOG_EXCEPTION_DETAILS"))
        try:
            async with asyncio.timeout(0.5):
                while source.starts < 2:  # noqa: ASYNC110 - bounded observation of the public source
                    await asyncio.sleep(0.001)
            await ready(source)
            assert (await gateway.snapshot(request)).instruments[0].available
        finally:
            await gateway.close()

    asyncio.run(run())
    record = next(item for item in caplog.records if item.message == "Market source disconnected; reconnect scheduled")
    assert record.data == {"exception_types": ["DiagnosticError"], "error_codes": []}
    assert "MUST_NOT_LOG" not in caplog.text
    assert "MUST_NOT_LOG" not in str(record.data)


def test_authorization_failure_waits_for_changed_configuration():
    class UnauthorizedSource(Source):
        async def start(self):
            await super().start()
            raise BrokerOperationError("BROKER_AUTH_FAILED", "synthetic", retryable=False)

    async def run():
        rejected, replacement = UnauthorizedSource(), Source()
        configuration = ["rejected"]
        gateway = MarketSnapshotGateway(
            lambda item: rejected if item == "rejected" else replacement,
            configuration_resolver=lambda _: configuration[0],
            now=lambda: NOW,
            retry_seconds=0.001,
        )
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        try:
            await gateway.snapshot(request)
            await asyncio.sleep(0.02)
            assert rejected.starts == 1
            assert not (await gateway.snapshot(request)).instruments[0].available
            configuration[0] = "replacement"
            await gateway.snapshot(request)
            await ready(replacement)
            assert (await gateway.snapshot(request)).instruments[0].available
        finally:
            await gateway.close()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["start", "stream"])
def test_repeated_failures_use_requested_intervals_without_parallel_connections(phase):
    class FailingSource(Source):
        async def start(self):
            assert self.starts == self.closes
            await super().start()
            if self.starts <= 5:
                if phase == "start":
                    raise ConnectionError("synthetic")
                await self.queue.put(ConnectionError("synthetic"))

    async def run():
        delays = []

        async def sleep(delay):
            delays.append(delay)
            await asyncio.sleep(0)

        source = FailingSource()
        gateway = MarketSnapshotGateway(
            lambda _: source,
            now=lambda: NOW,
            recovery=MarketRecoveryPolicy(retry_seconds=1, sleep=sleep),
        )
        try:
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",)))
            async with asyncio.timeout(0.2):
                while source.starts < 6:  # noqa: ASYNC110 - bounded observation of the source
                    await asyncio.sleep(0.001)
            await ready(source)
            await settle()
            assert delays == [1, 3, 5, 7, 9]
            assert source.starts == 6
        finally:
            await gateway.close()

    asyncio.run(run())


@pytest.mark.parametrize("limit", [0, 2, 5])
def test_transient_outage_recovers_after_budget_and_polling_cannot_bypass_cooldown(limit):
    class FailedSource(Source):
        broken = True

        async def start(self):
            assert self.starts == self.closes
            await super().start()
            if self.broken:
                raise ConnectionError("synthetic")

    async def run():
        delays = []
        cooldown = asyncio.Event()
        release = asyncio.Event()

        async def sleep(delay):
            delays.append(delay)
            if delay == 60:
                cooldown.set()
                await release.wait()
                release.clear()
            await asyncio.sleep(0)

        source = FailedSource()
        gateway = MarketSnapshotGateway(
            lambda _: source, now=lambda: NOW, recovery=MarketRecoveryPolicy(retry_limit=limit, sleep=sleep)
        )
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        try:
            await gateway.snapshot(request)
            await asyncio.wait_for(cooldown.wait(), 0.2)
            for _ in range(3):
                assert not (await gateway.snapshot(request)).instruments[0].available
                await settle()
            assert source.starts == source.closes == limit + 1
            assert delays == [*[1, 3, 5, 7, 9][:limit], 60]
            cooldown.clear()
            release.set()
            await asyncio.wait_for(cooldown.wait(), 0.2)
            assert source.starts == source.closes == limit + 2
            assert delays[-2:] == [60, 60]
            source.broken = False
            release.set()
            await ready(source)
            assert (await gateway.snapshot(request)).instruments[0].available
            assert source.starts == limit + 3
        finally:
            await gateway.close()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["shutdown", "rotation"])
def test_cooldown_can_be_cancelled_without_restarting_old_source(action):
    class FailedSource(Source):
        async def start(self):
            await super().start()
            raise ConnectionError("synthetic")

    async def run():
        cooldown = asyncio.Event()
        cancelled = asyncio.Event()

        async def sleep(delay):
            assert delay == 60
            cooldown.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        source, replacement = FailedSource(), Source()
        configuration = ["original"]
        gateway = MarketSnapshotGateway(
            lambda item: source if item == "original" else replacement,
            configuration_resolver=lambda _: configuration[0],
            now=lambda: NOW,
            recovery=MarketRecoveryPolicy(retry_limit=0, sleep=sleep),
        )
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        try:
            await gateway.snapshot(request)
            await asyncio.wait_for(cooldown.wait(), 0.2)
            if action == "rotation":
                configuration[0] = "replacement"
                await gateway.snapshot(request)
                await ready(replacement)
                assert (await gateway.snapshot(request)).instruments[0].available
            else:
                await asyncio.wait_for(gateway.close(), 0.2)
            assert cancelled.is_set()
            assert source.starts == source.closes == 1
        finally:
            await gateway.close()

    asyncio.run(run())


def test_fresh_complete_market_resets_retry_budget_after_recovery():
    class InitiallyFailedSource(Source):
        async def start(self):
            await super().start()
            if self.starts == 1:
                raise ConnectionError("synthetic")

    async def run():
        delays = []

        async def sleep(delay):
            delays.append(delay)
            await asyncio.sleep(0)

        source = InitiallyFailedSource()
        gateway = MarketSnapshotGateway(
            lambda _: source, now=lambda: NOW, recovery=MarketRecoveryPolicy(retry_limit=1, sleep=sleep)
        )
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        try:
            await gateway.snapshot(request)
            async with asyncio.timeout(0.2):
                while source.starts < 2:  # noqa: ASYNC110 - bounded observation of the source
                    await asyncio.sleep(0.001)
            await ready(source)
            assert (await gateway.snapshot(request)).instruments[0].available
            await source.queue.put(ConnectionError("synthetic"))
            async with asyncio.timeout(0.2):
                while source.starts < 3:  # noqa: ASYNC110 - bounded observation of the source
                    await asyncio.sleep(0.001)
            assert delays == [1, 1]
            assert not (await gateway.snapshot(request)).instruments[0].available
            await ready(source)
            assert (await gateway.snapshot(request)).instruments[0].available
            assert source.history_calls == ["AAA", "AAA"]
        finally:
            await gateway.close()

    asyncio.run(run())


@pytest.mark.parametrize("defect", ["empty_asks", "crossed_book", "zero_quantity", "trade_disabled", "future_status"])
def test_incomplete_or_untradeable_market_cannot_reset_retry_budget(defect):
    async def run():
        source = Source()

        delays = []

        async def sleep(delay):
            delays.append(delay)
            if delay == 60:
                await asyncio.Event().wait()
            await asyncio.sleep(0)

        gateway = MarketSnapshotGateway(
            lambda _: source, now=lambda: NOW, recovery=MarketRecoveryPolicy(retry_limit=2, sleep=sleep)
        )
        try:
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",)))
            for _ in range(3):
                await source.queue.put(
                    StreamOrderBook(
                        "AAA",
                        (OrderBookLevel(Decimal("10"), 0 if defect == "zero_quantity" else 1),),
                        (
                            ()
                            if defect == "empty_asks"
                            else (OrderBookLevel(Decimal("9" if defect == "crossed_book" else "11"), 1),)
                        ),
                        NOW,
                        True,
                    )
                )
                await source.queue.put(
                    StreamTradingStatus(
                        "AAA",
                        "NORMAL_TRADING",
                        defect != "trade_disabled",
                        True,
                        NOW + timedelta(seconds=1) if defect == "future_status" else NOW,
                    )
                )
                await settle()
                await source.queue.put(ConnectionError("synthetic"))
                await settle()
            assert source.starts == 3
            assert delays == [1, 3, 60]
        finally:
            await gateway.close()

    asyncio.run(run())


def test_history_transient_failure_recovers_after_cooldown():
    class FailedHistorySource(Source):
        broken = True

        async def get_candles(self, instrument_id, start, end):
            if self.broken:
                self.history_calls.append(instrument_id)
                raise ConnectionError("synthetic")
            return await super().get_candles(instrument_id, start, end)

    async def run():
        source = FailedHistorySource()
        clock = [NOW]
        gateway = MarketSnapshotGateway(
            lambda _: source, now=lambda: clock[0], retry_limit=2, retry_seconds=0.001, refresh_seconds=0.001
        )
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        try:
            await gateway.snapshot(request)
            await settle()
            for _ in range(6):
                clock[0] += timedelta(seconds=1)
                await asyncio.sleep(0.005)
            assert source.history_calls == ["AAA"] * 3
            assert source.starts == 1
            assert not (await gateway.snapshot(request)).instruments[0].available
            source.broken = False
            clock[0] += timedelta(seconds=60)
            await source.queue.put(
                StreamOrderBook(
                    "AAA", (OrderBookLevel(Decimal("10"), 1),), (OrderBookLevel(Decimal("11"), 1),), clock[0], True
                )
            )
            await source.queue.put(StreamTradingStatus("AAA", "NORMAL_TRADING", True, True, clock[0]))
            # Advance virtual time; adding an instrument wakes the existing history owner.
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA", "BBB")))
            await settle()
            assert (await gateway.snapshot(request)).instruments[0].available
            assert source.history_calls.count("AAA") == 4
            assert source.starts == 1
        finally:
            await gateway.close()

    asyncio.run(run())


def test_history_retry_budget_survives_stream_reconnects_with_a_healthy_peer():
    class FailedHistorySource(Source):
        broken = True

        async def get_candles(self, instrument_id, start, end):
            if instrument_id == "AAA" and self.broken:
                self.history_calls.append(instrument_id)
                raise ConnectionError("synthetic")
            return await super().get_candles(instrument_id, start, end)

    async def run():
        source = FailedHistorySource()
        clock = [NOW]

        async def sleep(_):
            clock[0] += timedelta(seconds=10)
            await asyncio.sleep(0)

        gateway = MarketSnapshotGateway(
            lambda _: source, now=lambda: clock[0], recovery=MarketRecoveryPolicy(retry_limit=2, sleep=sleep)
        )
        try:
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA", "BBB")))
            for _ in range(4):
                await source.queue.put(
                    StreamOrderBook(
                        "BBB", (OrderBookLevel(Decimal("10"), 1),), (OrderBookLevel(Decimal("11"), 1),), clock[0], True
                    )
                )
                await source.queue.put(StreamTradingStatus("BBB", "NORMAL_TRADING", True, True, clock[0]))
                await settle()
                await source.queue.put(ConnectionError("synthetic"))
                await settle()
            assert source.history_calls.count("AAA") == 3
            assert source.starts == 5
            source.broken = False
            clock[0] += timedelta(seconds=60)
            for instrument_id in ("AAA", "BBB"):
                await source.queue.put(
                    StreamOrderBook(
                        instrument_id,
                        (OrderBookLevel(Decimal("10"), 1),),
                        (OrderBookLevel(Decimal("11"), 1),),
                        clock[0],
                        True,
                    )
                )
                await source.queue.put(StreamTradingStatus(instrument_id, "NORMAL_TRADING", True, True, clock[0]))
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA", "BBB", "CCC")))
            await settle()
            snapshot = await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA", "BBB")))
            assert all(item.available for item in snapshot.instruments)
            assert source.history_calls.count("AAA") == 4
            assert source.starts == 5
        finally:
            await gateway.close()

    asyncio.run(run())


@pytest.mark.parametrize("fresh_peer", [True, False])
def test_only_fresh_source_data_prevents_quiet_reconnect(fresh_peer):
    class AcknowledgedSource(Source):
        def __init__(self):
            super().__init__()
            self.consumed = asyncio.Queue()

        async def events(self):
            async for item in super().events():
                yield item
                self.consumed.put_nowait(item)

    async def run():
        source = AcknowledgedSource()
        loop = asyncio.get_running_loop()
        real_time, clock = loop.time, [0.0]
        gateway = MarketSnapshotGateway(
            lambda _: source,
            now=lambda: NOW,
            recovery=MarketRecoveryPolicy(retry_seconds=0.001, quiet_seconds=0.01),
        )
        loop.time = lambda: clock[0]
        try:
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA", "BBB")))
            await settle()
            for index in range(12):
                clock[0] = index * 0.005
                at = NOW if fresh_peer else NOW - timedelta(minutes=1)
                await source.queue.put(
                    StreamOrderBook(
                        "BBB",
                        (OrderBookLevel(Decimal("10"), index + 1),),
                        (OrderBookLevel(Decimal("11"), 1),),
                        at,
                        True,
                    )
                )
                await pump_until(lambda: not source.consumed.empty())
                source.consumed.get_nowait()
                await settle()
            assert (source.starts == 1) if fresh_peer else (source.starts >= 2)
        finally:
            loop.time = real_time
            await gateway.close()

    asyncio.run(run())


def test_fresh_source_reconnects_at_exact_quiet_deadline():
    async def run():
        source = Source()
        loop = asyncio.get_running_loop()
        real_time, clock = loop.time, [0.0]
        gateway = MarketSnapshotGateway(
            lambda _: source,
            now=lambda: NOW,
            recovery=MarketRecoveryPolicy(retry_seconds=0.001, quiet_seconds=0.01),
        )
        loop.time = lambda: clock[0]
        try:
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",)))
            await ready(source)
            clock[0] = 0.009
            await settle()
            assert source.starts == 1
            clock[0] = 0.01
            await settle()
            # The watchdog has detected quietness at equality; retry remains due.
            assert source.closes == 1
            assert source.starts == 1
            clock[0] = 0.012
            await pump_until(lambda: source.starts == 2)
        finally:
            loop.time = real_time
            await gateway.close()

    asyncio.run(run())


@pytest.mark.parametrize("defect", ["empty_asks", "crossed_book"])
def test_continuously_arriving_invalid_books_do_not_suppress_quiet_recovery(defect):
    async def run():
        source = Source()
        gateway = MarketSnapshotGateway(
            lambda _: source,
            now=lambda: NOW,
            recovery=MarketRecoveryPolicy(retry_seconds=0.001, quiet_seconds=0.01),
        )
        try:
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",)))
            for index in range(12):
                await source.queue.put(
                    StreamOrderBook(
                        "AAA",
                        (OrderBookLevel(Decimal("10"), index + 1),),
                        () if defect == "empty_asks" else (OrderBookLevel(Decimal("9"), 1),),
                        NOW,
                        True,
                    )
                )
                await asyncio.sleep(0.003)
            assert source.starts >= 2
        finally:
            await gateway.close()

    asyncio.run(run())


def test_history_timeout_isolates_instrument_and_preserves_fresh_peer():
    class HangingHistorySource(Source):
        async def get_candles(self, instrument_id, start, end):
            if instrument_id == "AAA":
                await asyncio.Event().wait()
            return await super().get_candles(instrument_id, start, end)

    async def run():
        source = HangingHistorySource()
        gateway = MarketSnapshotGateway(
            lambda _: source,
            now=lambda: NOW,
            recovery=MarketRecoveryPolicy(operation_timeout_seconds=0.005),
        )
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA", "BBB"))
        try:
            await gateway.snapshot(request)
            await source.queue.put(StreamOrderBook("BBB", (OrderBookLevel(Decimal("10"), 1),), (), NOW, True))
            await source.queue.put(StreamTradingStatus("BBB", "NORMAL_TRADING", True, True, NOW))
            await asyncio.sleep(0.02)
            result = await gateway.snapshot(request)
            assert not result.instruments[0].available
            assert result.instruments[1].available
            assert source.starts == 1
        finally:
            await gateway.close()

    asyncio.run(run())


def test_permanent_history_error_is_not_retried_while_peer_keeps_refreshing():
    class RejectedHistorySource(Source):
        async def get_candles(self, instrument_id, start, end):
            if instrument_id == "AAA":
                self.history_calls.append(instrument_id)
                raise BrokerOperationError("BROKER_FORBIDDEN", "synthetic", retryable=False)
            return await super().get_candles(instrument_id, start, end)

    async def run():
        source = RejectedHistorySource()
        clock = [NOW]
        gateway = MarketSnapshotGateway(
            lambda _: source, now=lambda: clock[0], retry_seconds=0.001, refresh_seconds=0.001
        )
        try:
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA", "BBB")))
            await settle()
            for _ in range(3):
                clock[0] += timedelta(seconds=1)
                await asyncio.sleep(0.003)
            assert source.history_calls.count("AAA") == 1
            assert source.history_calls.count("BBB") >= 2
            assert source.starts == 1
        finally:
            await gateway.close()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["start", "subscription", "history", "close"])
def test_hanging_operations_are_cancelled_before_reconnect_or_shutdown(operation):
    class HangingSource(Source):
        def __init__(self):
            super().__init__()
            self.cancelled = asyncio.Event()
            self.entered = asyncio.Event()

        async def hang(self):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

        async def start(self):
            await super().start()
            if operation == "start" and self.starts == 1:
                await self.hang()

        async def replace_subscriptions(self, instruments):
            if operation == "subscription" and self.starts == 1:
                await self.hang()
            await super().replace_subscriptions(instruments)

        async def get_candles(self, instrument_id, start, end):
            if operation == "history" and not self.cancelled.is_set():
                await self.hang()
            return await super().get_candles(instrument_id, start, end)

        async def close(self):
            await super().close()
            if operation == "close":
                await self.hang()

    async def run():
        source = HangingSource()
        loop = asyncio.get_running_loop()
        real_time, clock = loop.time, [0.0]
        gateway = MarketSnapshotGateway(
            lambda _: source,
            now=lambda: NOW,
            recovery=MarketRecoveryPolicy(
                retry_seconds=0.001, operation_timeout_seconds=0.005, close_timeout_seconds=0.005
            ),
        )
        close_task = None
        loop.time = lambda: clock[0]
        try:
            request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
            await gateway.snapshot(request)
            await ready(source)
            if operation == "close":
                close_task = asyncio.create_task(gateway.close())
            await pump_until(source.entered.is_set)
            # Pass the actual operation deadline, remaining below the owner deadline.
            clock[0] = 0.006
            await pump_until(source.cancelled.is_set)
            if close_task is not None:
                await pump_until(close_task.done)
                await close_task
            assert source.cancelled.is_set()
        finally:
            loop.time = real_time
            if close_task is not None and not close_task.done():
                await close_task
            await gateway.close()

    asyncio.run(run())


@pytest.mark.parametrize("closed_market", [False, True])
def test_quiet_stream_reconnects_unless_market_explicitly_closed(closed_market):
    async def run():
        source = Source()
        gateway = MarketSnapshotGateway(
            lambda _: source,
            now=lambda: NOW,
            recovery=MarketRecoveryPolicy(retry_seconds=0.001, quiet_seconds=0.01),
        )
        try:
            await gateway.snapshot(MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",)))
            if closed_market:
                await source.queue.put(StreamTradingStatus("AAA", "NOT_AVAILABLE_FOR_TRADING", False, False, NOW))
            await settle()
            await asyncio.sleep(0.025)
            assert (source.starts == 1) if closed_market else (source.starts >= 2)
        finally:
            await gateway.close()

    asyncio.run(run())


def test_cancel_resistant_source_prevents_replacement_and_close_returns_bounded():
    class ResistantSource(Source):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()
            self.cancel_received = asyncio.Event()

        async def start(self):
            await super().start()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_received.set()
                await self.release.wait()

    async def run():
        source, replacement = ResistantSource(), Source()
        configuration = ["original"]
        gateway = MarketSnapshotGateway(
            lambda item: source if item == "original" else replacement,
            now=lambda: NOW,
            configuration_resolver=lambda _: configuration[0],
            recovery=MarketRecoveryPolicy(operation_timeout_seconds=0.005, close_timeout_seconds=0.005),
        )
        request = MarketSnapshotRequest(source_id=SOURCE, instrument_ids=("AAA",))
        try:
            await gateway.snapshot(request)
            await asyncio.wait_for(source.cancel_received.wait(), 0.2)
            assert not (await gateway.snapshot(request)).instruments[0].available
            configuration[0] = "replacement"
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(gateway.snapshot(request), 0.1)
            assert source.closes == 0
            assert replacement.starts == 0
            source.release.set()
            await settle()
            await gateway.snapshot(request)
            await ready(replacement)
            assert (await gateway.snapshot(request)).instruments[0].available
        finally:
            source.release.set()
            await gateway.close()

    asyncio.run(run())
