"""Partial terminal fills keep recovery financial checks and ledger identities."""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.broker_execution import BrokerOrderState, BrokerPosition, OrderSide
from tests.trading_automaton.command_factory import command, decision_item
from tests.trading_automaton.storage.worker_storage_helpers import NOW
from trading_automaton.domain.dtos import DispatchRequest
from trading_automaton.services.order_tracking import OrderTrackingService
from trading_automaton.services.uncertain_intent_reconciliation import UncertainIntentReconciliationService
from trading_automaton.storage.database import create_worker_engine, initialize_worker_schema
from trading_automaton.storage.models import FactOutboxModel, LocalIntentModel
from trading_automaton.storage.repository import LocalAutomationRepository


@pytest.fixture
def runtime(tmp_path):
    engine = create_worker_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    initialize_worker_schema(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repo = LocalAutomationRepository(factory, environment="PROD")
    value = command(lot_size=1)
    repo.cache_command(value)
    item = decision_item(value)
    item = item.model_copy(
        update={"intent": item.intent.model_copy(update={"quantity_lots": 2}), "decision_quantity_lots": 2}
    )
    intent = repo.save_decision_batch((item,), occurred_at=NOW).intents[0]
    repo.update_intent(intent.idempotency_key, state="UNCERTAIN", occurred_at=NOW)
    yield repo, factory, value, intent
    engine.dispose()


def response(intent, status, *, executed_lots=1, price=100):
    return BrokerOrderState(
        "order",
        intent.idempotency_key,
        status,
        2,
        executed_lots,
        Decimal(200),
        Decimal(executed_lots * 100),
        Decimal(1),
        Decimal(executed_lots),
        "RUB",
        executed_price=Decimal(price),
        executed_at=NOW if executed_lots else None,
    )


class Broker:
    def __init__(self, observed, position_lots):
        self.observed, self.position_lots = observed, position_lots

    async def find_by_idempotency_key(self, account_id, idempotency_key):
        assert idempotency_key == self.observed.idempotency_key
        return self.observed

    async def inspect_position(self, account_id, instrument_id):
        return BrokerPosition(instrument_id, Decimal(self.position_lots), Decimal(100), Decimal(100), "RUB")


def recover(runtime, observed, position_lots):
    repo, _, value, _ = runtime
    service = UncertainIntentReconciliationService(
        repo, Broker(observed, position_lots), now=lambda: NOW, environment="PROD"
    )
    return asyncio.run(service.reconcile((value,)))


def assert_no_financial_result(runtime):
    repo, factory, value, intent = runtime
    assert repo.list_open_lots(str(value.automation_id)) == []
    with factory() as session:
        assert session.get(LocalIntentModel, intent.idempotency_key).terminal_at is None
        kinds = session.scalars(select(FactOutboxModel.fact_kind)).all()
        assert "TRADE_EXECUTION_RECORDED" not in kinds
        assert "POSITION_LOT_OPENED" not in kinds
    assert repo.get_state(str(value.automation_id)).state == "HOLD"


@pytest.mark.parametrize("status", ["EXPIRED", "CANCELLED", "REJECTED", "FILLED"])
def test_inconsistent_partial_terminal_position_holds_without_financial_rows(runtime, status):
    result = recover(runtime, response(runtime[3], status), 999)
    assert result.unresolved == 1
    assert_no_financial_result(runtime)


@pytest.mark.parametrize("status", ["EXPIRED", "CANCELLED", "REJECTED", "FILLED"])
def test_partial_terminal_missing_price_holds_without_finalization(runtime, status):
    result = recover(runtime, response(runtime[3], status, price=0), 1)
    assert result.unresolved == 1
    assert_no_financial_result(runtime)


@pytest.mark.parametrize("status", ["EXPIRED", "CANCELLED", "REJECTED", "FILLED"])
def test_tracking_partial_terminal_missing_price_holds_without_finalization(runtime, status):
    repo, _, value, intent = runtime
    request = DispatchRequest(
        intent.idempotency_key,
        value.account_id,
        value.external_instrument_id,
        OrderSide.BUY,
        2,
        Decimal(100),
        automation_id=str(value.automation_id),
        lot_size=1,
        broker_id=str(value.broker_id),
    )

    async def track():
        tracking = OrderTrackingService(repo, now=lambda: NOW)
        tracking.track(
            intent.idempotency_key,
            asyncio.create_task(asyncio.sleep(0, result=response(intent, status, price=0))),
            request=request,
        )
        await tracking.wait_all()

    asyncio.run(track())
    assert_no_financial_result(runtime)


@pytest.mark.parametrize("executed_lots", [0, 1])
def test_zero_or_valid_partial_expired_result_finalizes(runtime, executed_lots):
    result = recover(
        runtime,
        response(runtime[3], "EXPIRED", executed_lots=executed_lots, price=100 if executed_lots else 0),
        executed_lots,
    )
    assert result.resolved == 1
    repo, factory, value, intent = runtime
    assert sum(lot.remaining_lots for lot in repo.list_open_lots(str(value.automation_id))) == executed_lots
    with factory() as session:
        assert session.get(LocalIntentModel, intent.idempotency_key).terminal_at == NOW


@pytest.mark.parametrize("status", ["EXPIRED", "CANCELLED", "REJECTED"])
def test_already_applied_partial_terminal_preserves_ledger(runtime, status):
    repo, _, value, intent = runtime
    repo.create_trade_lot(
        automation_id=str(value.automation_id),
        source_intent_id=intent.idempotency_key,
        source="EXECUTED",
        quantity_lots=1,
        entry_price=Decimal(100),
        entry_commission=Decimal(1),
        opened_at=NOW,
    )
    before = repo.list_open_lots(str(value.automation_id))
    assert recover(runtime, response(intent, status), 1).resolved == 1
    assert repo.list_open_lots(str(value.automation_id)) == before


def test_matching_partial_position_requires_evidence_for_this_intent(runtime):
    repo, factory, value, intent = runtime
    repo.create_trade_lot(
        automation_id=str(value.automation_id),
        source_intent_id=None,
        source="BOOTSTRAP",
        quantity_lots=1,
        entry_price=Decimal(100),
        entry_commission=Decimal(0),
        opened_at=NOW,
    )
    before = repo.list_open_lots(str(value.automation_id))
    with pytest.raises(ValueError, match="no pre-applied ledger evidence"):
        recover(runtime, response(intent, "EXPIRED"), 1)
    assert repo.list_open_lots(str(value.automation_id)) == before
    with factory() as session:
        assert session.get(LocalIntentModel, intent.idempotency_key).terminal_at is None
        assert "TRADE_EXECUTION_RECORDED" not in session.scalars(select(FactOutboxModel.fact_kind)).all()
