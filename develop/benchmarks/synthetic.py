"""Deterministic market and broker boundaries for the runtime benchmark."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sentinel_contracts.analytics import MarketSourceSnapshot
from sentinel_contracts.broker_execution import BrokerOrderState, BrokerPosition
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.dtos import CommissionQuote

START = datetime(2026, 9, 9, 9, tzinfo=UTC)


def identifier(value):
    return uuid5(NAMESPACE_URL, f"sentinel-benchmark:{value}")


def commands(count):
    return tuple(
        AutomationCommand(
            automation_id=identifier(f"automation-{index}"),
            user_broker_id=identifier("user-broker"),
            broker_id=identifier("broker"),
            account_id="synthetic-account",
            external_instrument_id=f"synthetic-{index}",
            instrument_id=identifier(f"instrument-{index}"),
            currency="RUB",
            lot_size=10,
            min_price_increment=Decimal("0.01"),
            state="IN_WORK",
            revision=1,
            last_sequence_number=0,
            resume_requested=False,
        )
        for index in range(count)
    )


class SyntheticMarket:
    def __init__(self, values):
        self.values = values
        self.now = START
        self.generation = 0
        self.requests = 0

    def advance(self):
        self.generation += 1
        self.now = START + timedelta(minutes=self.generation)

    async def snapshot(self, request):
        self.requests += 1
        return MarketSourceSnapshot(
            snapshot_id=f"generation-{self.generation}",
            captured_at=self.now,
            instruments=[self.instrument(value.external_instrument_id) for value in self.values],
        )

    def instrument(self, name):
        return {
            "instrument_id": name,
            "available": True,
            "market": {
                "instrument_id": name,
                "order_book": {
                    "instrument_id": name,
                    "bids": [{"price": "100", "quantity_lots": 1000}],
                    "asks": [{"price": "101", "quantity_lots": 1000}],
                    "captured_at": self.now,
                    "is_consistent": True,
                },
                "trading_status": {
                    "instrument_id": name,
                    "status": "NORMAL",
                    "limit_order_available": True,
                    "api_trade_available": True,
                    "captured_at": self.now,
                },
            },
            "candles": [
                {
                    "instrument_id": name,
                    "open": str(100 + index),
                    "high": str(102 + index),
                    "low": str(99 + index),
                    "close": str(101 + index),
                    "volume": 10,
                    "started_at": self.now - timedelta(minutes=20 - index),
                    "is_complete": True,
                    "captured_at": self.now,
                }
                for index in range(20)
            ],
        }


class SyntheticBroker:
    def __init__(self, values, market):
        self.values = values
        self.market = market
        self.orders = {}
        self.quantities = {value.external_instrument_id: 0 for value in values}

    async def start(self):
        pass

    async def close(self):
        pass

    async def get_positions(self, account_id):
        return tuple(
            BrokerPosition(name, Decimal(quantity), Decimal(101), Decimal(100), "RUB")
            for name, quantity in self.quantities.items()
        )

    async def get_free_cash(self, account_id, currency):
        return Decimal(1000000)

    async def quote(self, request, side):
        return CommissionQuote(Decimal(1010), Decimal(1))

    async def dispatch_limit_order(self, request):
        if request.idempotency_key in self.orders:
            raise AssertionError("Duplicate dispatch after terminal fill")
        if request.side != "BUY":
            raise AssertionError("Unexpected order in constant-market workload")
        self.quantities[request.instrument_id] += request.quantity_lots
        amount = request.limit_price * request.quantity_lots * request.lot_size
        result = BrokerOrderState(
            broker_order_id=str(identifier(f"order-{len(self.orders)}")),
            idempotency_key=request.idempotency_key,
            status="FILLED",
            requested_lots=request.quantity_lots,
            executed_lots=request.quantity_lots,
            requested_amount=amount,
            executed_amount=amount,
            estimated_commission=Decimal(1),
            executed_commission=Decimal(1),
            currency="RUB",
            executed_price=request.limit_price,
            executed_at=self.market.now,
        )
        self.orders[request.idempotency_key] = result
        return result
