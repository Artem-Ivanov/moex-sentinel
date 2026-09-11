"""OrderState contains an aggregate amount; PostOrderResponse contains a unit price."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from moex_sentinel.adapters.tinvest.order_execution import TInvestOrderExecutionAdapter
from moex_sentinel.adapters.tinvest.portfolio import SANDBOX_TARGET
from sentinel_contracts.broker_execution import OrderSide
from tests.trading_automaton.command_factory import decision_item
from tests.trading_automaton.storage.test_local_repository import (
    NOW,
    SELL_INTENT_ID,
    baseline_command,
    filled_buy,
    repository,
)
from trading_automaton.adapters.tinvest_broker_session import BrokerSdkSession
from trading_automaton.domain.dtos import DispatchRequest
from trading_automaton.storage.repository import IntentBatchItem


def money(value):
    amount = Decimal(value)
    units = int(amount)
    return SimpleNamespace(units=units, nano=int((amount - units) * 1_000_000_000), currency="rub")


def response(*, total="2340", lots=3):
    return SimpleNamespace(
        order_id="synthetic-order",
        order_request_id="synthetic-intent",
        execution_report_status="EXECUTION_REPORT_STATUS_FILL",
        lots_requested=lots,
        lots_executed=lots,
        initial_order_price=money(total),
        executed_order_price=money(total),
        total_order_amount=money(total),
        initial_commission=money("1.17"),
        executed_commission=money("1.17"),
        average_position_price=money("7.8"),
        stages=(),
        order_date=datetime(2026, 9, 9, tzinfo=UTC),
    )


async def fetch_state(adapter_kind, operation, value):
    class Orders:
        async def post_order(self, **kwargs):
            return value

        async def get_order_state(self, **kwargs):
            return value

        async def get_orders(self, **kwargs):
            return SimpleNamespace(orders=[value])

    class Context:
        async def __aenter__(self):
            return SimpleNamespace(orders=Orders())

        async def __aexit__(self, *args):
            pass

    def factory(*args, **kwargs):
        return Context()

    adapter = (
        BrokerSdkSession("synthetic-token", SANDBOX_TARGET, client_factory=factory)
        if adapter_kind == "worker"
        else TInvestOrderExecutionAdapter("synthetic-token", SANDBOX_TARGET, client_factory=factory)
    )
    if adapter_kind == "worker":
        await adapter.start()
    try:
        if operation == "get":
            return await adapter.get_order_state("synthetic-account", "synthetic-order")
        if operation == "find":
            return await adapter.find_by_idempotency_key("synthetic-account", "synthetic-intent")
        if adapter_kind == "worker":
            return await adapter.dispatch_limit_order(
                DispatchRequest(
                    "synthetic-intent", "synthetic-account", "synthetic-share", OrderSide.SELL, 3, Decimal("7.8")
                )
            )
        return await adapter.submit_limit_order(
            "synthetic-account", "synthetic-share", OrderSide.SELL, 3, Decimal("7.8"), "synthetic-intent"
        )
    finally:
        if adapter_kind == "worker":
            await adapter.close()


@pytest.mark.parametrize("adapter_kind", ["worker", "core"])
@pytest.mark.parametrize("operation", ["get", "find"])
@pytest.mark.parametrize(("lots", "total"), [(1, "780"), (3, "2340")])
def test_order_state_uses_unit_average_without_multiplying_lot_size_twice(adapter_kind, operation, lots, total):
    result = asyncio.run(fetch_state(adapter_kind, operation, response(total=total, lots=lots)))
    assert result.executed_price == Decimal("7.8")
    assert result.executed_amount == Decimal(total)
    assert result.executed_lots == lots


@pytest.mark.parametrize("adapter_kind", ["worker", "core"])
def test_post_order_preserves_unit_price(adapter_kind):
    value = response()
    value.executed_order_price = money("7.8")
    del value.average_position_price
    assert asyncio.run(fetch_state(adapter_kind, "post", value)).executed_price == Decimal("7.8")


@pytest.mark.parametrize("adapter_kind", ["worker", "core"])
def test_order_state_falls_back_to_weighted_unit_prices_of_all_filled_stages(adapter_kind):
    value = response()
    value.average_position_price = money("0")
    value.stages = (SimpleNamespace(price=money("7.5"), quantity=1), SimpleNamespace(price=money("7.95"), quantity=2))
    assert asyncio.run(fetch_state(adapter_kind, "get", value)).executed_price == Decimal("7.8")


@pytest.mark.parametrize("adapter_kind", ["worker", "core"])
def test_missing_unit_price_is_rejected_instead_of_using_aggregate_amount(adapter_kind):
    value = response()
    value.average_position_price = money("0")
    with pytest.raises(ValueError, match="unit price"):
        asyncio.run(fetch_state(adapter_kind, "get", value))


@pytest.mark.parametrize("adapter_kind", ["worker", "core"])
def test_incomplete_execution_stages_cannot_be_used_as_average_price(adapter_kind):
    value = response(lots=3)
    value.average_position_price = money("0")
    value.stages = (SimpleNamespace(price=money("7.8"), quantity=1),)
    with pytest.raises(ValueError, match="unit price"):
        asyncio.run(fetch_state(adapter_kind, "get", value))


@pytest.mark.parametrize("adapter_kind", ["worker", "core"])
@pytest.mark.parametrize("status", ["EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_CANCELLED"])
def test_unfilled_state_needs_no_execution_price(adapter_kind, status):
    value = response(total="0", lots=0)
    value.execution_report_status = status
    del value.average_position_price
    result = asyncio.run(fetch_state(adapter_kind, "get", value))
    assert result.executed_price == 0
    assert result.executed_lots == 0


@pytest.mark.parametrize("adapter_kind", ["worker", "core"])
def test_normalized_sell_price_reaches_lifo_net_pnl_without_amount_amplification(adapter_kind):
    repo, factory = repository()
    try:
        command = baseline_command()
        repo.cache_command(command)
        repo.save_decision_batch((decision_item(command),), occurred_at=NOW)
        repo.finalize_execution(filled_buy())
        sell = decision_item(command, intent_id=SELL_INTENT_ID).model_copy(
            update={
                "decision": "SELL_ALL",
                "limit_price": Decimal("110"),
                "intent": IntentBatchItem(SELL_INTENT_ID, "SELL_ALL", "SELL", 1, Decimal("110")),
            }
        )
        repo.save_decision_batch((sell,), occurred_at=NOW)
        value = response(total="1100", lots=1)
        value.average_position_price = money("110")
        value.executed_commission = money("1")
        normalized = asyncio.run(fetch_state(adapter_kind, "get", value))
        result = repo.finalize_execution(
            filled_buy().model_copy(
                update={
                    "intent_id": SELL_INTENT_ID,
                    "side": "SELL",
                    "requested_price": Decimal("110"),
                    "requested_amount": Decimal("1100"),
                    "executed_amount": normalized.executed_amount,
                    "executed_price": normalized.executed_price,
                    "executed_commission": normalized.executed_commission,
                }
            )
        )
        # 10 shares × (110 - 100), less entry commission 2 and exit commission 1.
        assert Decimal(result.authoritative_position_snapshot["net_pnl"]) == Decimal("97")
        assert Decimal(result.authoritative_position_snapshot["realized_pnl"]) == Decimal("97")
        assert repo.list_open_lots(str(command.automation_id)) == []
    finally:
        factory.kw["bind"].dispose()
