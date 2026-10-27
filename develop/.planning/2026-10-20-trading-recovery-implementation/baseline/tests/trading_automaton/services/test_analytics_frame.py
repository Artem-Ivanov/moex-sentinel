import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from sentinel_contracts.analytics import AnalyticsInstrument, AnalyticsSnapshot, MarketIndicators
from sentinel_contracts.broker_execution import BrokerConnection, OrderBookLevel, OrderSide
from sentinel_contracts.streaming_market import (
    InstrumentMarketState,
    StreamOrderBook,
    StreamTradingStatus,
    UnaryApiTradingStatus,
)
from sentinel_contracts.trading_admission import (
    PreRpcAbortProof,
    SubmitAuthorizationRequest,
    SubmitPermit,
    submit_request_fingerprint,
)
from trading_automaton.adapters.tinvest_broker_session import BrokerSdkSession
from trading_automaton.domain.dtos import DispatchRequest
from trading_automaton.services.analytics_frame import AnalyticsFrameService
from trading_automaton.services.order_book_validation import OrderBookValidationService
from trading_automaton.services.trading_admission import SubmitGate

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
BROKER_ID = "00000000-0000-4000-8000-000000000001"
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000002")
AUTOMATION_ID = UUID("00000000-0000-4000-8000-000000000004")
CONFIGURATION_FINGERPRINT = "a" * 64


def test_api_status_expiry_bounds_snapshot_and_prevents_post_after_authorization():
    status_expires_at = NOW + timedelta(seconds=1)
    book = StreamOrderBook(
        "instrument",
        (OrderBookLevel(Decimal("100"), 1),),
        (OrderBookLevel(Decimal("100.1"), 1),),
        NOW,
        True,
    )
    market = InstrumentMarketState(
        "instrument",
        order_book=book,
        trading_status=StreamTradingStatus("instrument", "NORMAL_TRADING", True, NOW),
        api_trading_status=UnaryApiTradingStatus(True, NOW, status_expires_at),
    )
    indicators = MarketIndicators(
        averaging_step_percent=Decimal("0.5"),
        minimum_net_profit_percent=Decimal("0.5"),
        source="TEST",
        mean_5=None,
        mean_20=None,
        change_10_percent=None,
        last_candle_at=None,
        last_candle_open=None,
        last_candle_close=None,
        range_low=None,
        range_high=None,
    )
    frame = AnalyticsSnapshot(
        snapshot_id="frame",
        captured_at=NOW,
        ttl_ms=2000,
        profile_id="profile",
        instruments=(
            AnalyticsInstrument(
                instrument_id="instrument",
                market=market,
                candles=(),
                available=True,
                metrics=indicators,
                freshness="FRESH",
            ),
        ),
    )
    frames = AnalyticsFrameService(SimpleNamespace(), now=lambda: NOW, order_books=OrderBookValidationService())
    accepted = frames.fresh_instruments(frame)
    snapshot = frames.market_snapshot(frame, accepted)
    assert snapshot.expires_at == status_expires_at

    request_id = uuid4()
    request = DispatchRequest(
        idempotency_key=str(request_id),
        account_id="account",
        instrument_id="instrument",
        side=OrderSide.BUY,
        quantity_lots=1,
        limit_price=Decimal("100.1"),
        automation_id=str(AUTOMATION_ID),
        lot_size=1,
        broker_id=BROKER_ID,
        reservation_currency="RUB",
        market_valid_until=snapshot.expires_at,
        submit_deadline=status_expires_at,
        admission_runtime_instance_id=str(RUNTIME_ID),
        admission_generation=1,
        admission_revision=1,
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
    )
    calls = []
    now_after_status_expiry = status_expires_at + timedelta(milliseconds=1)

    class Admission:
        async def authorize_submit(self, authorization):
            authorization_request = SubmitAuthorizationRequest(
                broker_id=UUID(authorization.broker_id),
                account_id=authorization.account_id,
                automation_id=UUID(authorization.automation_id),
                idempotency_key=request_id,
                generation=authorization.admission_generation,
                revision=authorization.admission_revision,
                configuration_fingerprint=authorization.configuration_fingerprint,
                instrument_uid=authorization.instrument_id,
                lot_size=authorization.lot_size,
                currency=authorization.reservation_currency,
                side=authorization.side.value,
                quantity_lots=authorization.quantity_lots,
                limit_price=authorization.limit_price,
                market_valid_until=authorization.market_valid_until,
                submit_deadline=authorization.submit_deadline,
            )
            permit = SubmitPermit(
                permit_id=uuid4(),
                runtime_instance_id=RUNTIME_ID,
                idempotency_key=request_id,
                generation=authorization_request.generation,
                revision=authorization_request.revision,
                configuration_fingerprint=authorization_request.configuration_fingerprint,
                request_fingerprint=submit_request_fingerprint(authorization_request),
                issued_at=NOW,
                server_response_at=NOW,
                expires_at=NOW + timedelta(seconds=10),
                risk_snapshot_id=uuid4(),
            )
            return SubmitGate(permit=permit, expires_monotonic=100)

        def abort_proof(self, dispatch, permit, reason):
            return PreRpcAbortProof(
                permit_id=permit.permit_id,
                idempotency_key=request_id,
                request_fingerprint=permit.request_fingerprint,
                runtime_instance_id=RUNTIME_ID,
                generation=permit.generation,
                configuration_fingerprint=CONFIGURATION_FINGERPRINT,
                aborted_at=now_after_status_expiry,
                reason=reason,
            )

    class Client:
        async def __aenter__(self):
            return SimpleNamespace(orders=SimpleNamespace(post_order=self.post_order))

        async def __aexit__(self, *_args):
            return None

        async def post_order(self, **_kwargs):
            calls.append("post_order")
            raise AssertionError("Expired unary trading status must stop before SDK post_order.")

    connection = BrokerConnection(
        broker_id=BROKER_ID,
        adapter_code="TINVEST_PROD",
        target="invest-public-api.tbank.ru:443",
        token="synthetic-token",  # noqa: S106 - deterministic credential for an offline test client.
        is_test=False,
        environment="PROD",
        access_mode="TRADE",
        account_id="account",
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
    )
    session = BrokerSdkSession(
        connection.token,
        connection.target,
        environment="PROD",
        access_mode="TRADE",
        account_id="account",
        broker_connection=connection,
        submit_admission=Admission(),
        client_factory=lambda *_args, **_kwargs: Client(),
        now=lambda: now_after_status_expiry,
        monotonic=lambda: 0,
    )

    async def run():
        await session.start()
        try:
            return await session.dispatch_limit_order(request)
        finally:
            await session.close()

    result = asyncio.run(run())

    assert result.status == "CANCELLED"
    assert result.pre_rpc_abort_proof is not None
    assert result.pre_rpc_abort_proof.reason == "MARKET_EXPIRED_BEFORE_POST"
    assert calls == []
