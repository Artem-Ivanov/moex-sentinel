"""Market snapshots and a recording source shared by Analytics contract tests."""

from datetime import UTC, datetime, timedelta

from sentinel_contracts.analytics import MarketSourceSnapshot

NOW = datetime(2026, 9, 9, 9, tzinfo=UTC)


def instrument(identifier="instrument", *, age_ms=0, valid=True):
    return {
        "instrument_id": identifier,
        "available": True,
        "market": {
            "instrument_id": identifier,
            "order_book": {
                "instrument_id": identifier,
                "bids": [{"price": "100", "quantity_lots": 2}],
                "asks": [{"price": "101" if valid else "99", "quantity_lots": 2}],
                "captured_at": NOW - timedelta(milliseconds=age_ms),
                "is_consistent": True,
            },
            "trading_status": {
                "instrument_id": identifier,
                "status": "NORMAL",
                "limit_order_available": True,
                "api_trade_available": True,
                "captured_at": NOW - timedelta(hours=1),
            },
        },
        "candles": [
            {
                "instrument_id": identifier,
                "open": str(100 + index),
                "high": str(102 + index),
                "low": str(99 + index),
                "close": str(101 + index),
                "volume": 10,
                "started_at": NOW - timedelta(minutes=20 - index),
                "is_complete": True,
                "captured_at": NOW,
            }
            for index in range(20)
        ],
    }


class Source:
    def __init__(self, instruments=None, *, captured_at=NOW, error=None):
        self.calls = []
        self.instruments = [instrument()] if instruments is None else instruments
        self.captured_at = captured_at
        self.error = error

    async def snapshot(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return MarketSourceSnapshot(
            snapshot_id="generation-1", captured_at=self.captured_at, instruments=self.instruments
        )
