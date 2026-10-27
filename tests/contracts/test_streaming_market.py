from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sentinel_contracts.broker_execution import OrderBookLevel
from sentinel_contracts.streaming_market import (
    InstrumentMarketState,
    StreamOrderBook,
    StreamTradingStatus,
)

NOW = datetime(2026, 10, 18, 10, tzinfo=UTC)


def market(*, api_available: bool | None, observed_at: datetime = NOW, expires_at: datetime | None = None):
    return InstrumentMarketState(
        instrument_id="instrument",
        order_book=StreamOrderBook(
            instrument_id="instrument",
            bids=(OrderBookLevel(Decimal("99"), 1),),
            asks=(OrderBookLevel(Decimal("100"), 1),),
            captured_at=NOW,
            is_consistent=True,
        ),
        trading_status=StreamTradingStatus(
            instrument_id="instrument",
            status="NORMAL_TRADING",
            limit_order_available=True,
            captured_at=NOW,
        ),
        api_trading_status=(
            None
            if api_available is None
            else {
                "api_trade_available": api_available,
                "observed_at": observed_at,
                "expires_at": expires_at or NOW + timedelta(seconds=30),
            }
        ),
    )


def test_market_is_tradeable_only_with_fresh_unary_api_availability():
    assert market(api_available=True).is_tradeable(NOW, timedelta(seconds=2))
    assert not market(api_available=None).is_tradeable(NOW, timedelta(seconds=2))
    assert not market(api_available=False).is_tradeable(NOW, timedelta(seconds=2))
    assert not market(api_available=True, expires_at=NOW).is_tradeable(NOW, timedelta(seconds=2))
    assert not market(api_available=True, observed_at=NOW + timedelta(milliseconds=1)).is_tradeable(
        NOW, timedelta(seconds=2)
    )


def test_legacy_stream_api_flag_is_ignored_as_unknown():
    state = InstrumentMarketState.model_validate(
        {
            "instrument_id": "instrument",
            "trading_status": {
                "instrument_id": "instrument",
                "status": "NORMAL_TRADING",
                "limit_order_available": True,
                "api_trade_available": True,
                "captured_at": NOW,
            },
        }
    )

    assert state.trading_status is not None
    assert not hasattr(state.trading_status, "api_trade_available")
    assert state.api_trading_status is None
    assert not state.is_tradeable(NOW, timedelta(seconds=2))
