from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.execution_commissions import BrokerFeeObservation
from moex_sentinel.domain.portfolio import ExternalOperation, Money, OperationsPage
from moex_sentinel.services.execution_commissions import ExecutionCommissionService
from moex_sentinel.storage.models.execution_commissions import (
    BrokerFeeScanStateModel,
    BrokerOrderFeeObservationModel,
)
from moex_sentinel.storage.models.position_facts import ExecutionLotAllocationModel, PositionLotModel
from moex_sentinel.storage.models.automation_facts import PositionCycleModel
from moex_sentinel.storage.models.order_facts import BrokerOrderModel, TradeExecutionModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel
from moex_sentinel.storage.repositories.automations import AutomationRepository
from moex_sentinel.storage.repositories.execution_commissions import ExecutionCommissionRepository
from tests.storage.trading_facts_helpers import NOW, decision_model, execution_model, order_model, seed_order


def ready_repository(
    core_database: tuple[object, Session],
) -> tuple[sessionmaker[Session], ExecutionCommissionRepository]:
    engine, session = core_database
    seed_order(session)
    broker = session.get(UserBrokerModel, "scope-1")
    broker.environment = "PROD"
    broker.fqdn = "invest-public-api.tbank.ru:443"
    order = session.get(BrokerOrderModel, "order-1")
    order.external_order_id = "exchange-order-1"
    order.state = "EXPIRED"
    order.executed_amount = Decimal("10.25")
    order.executed_commission = Decimal("0.01")
    order.terminal_at = NOW
    session.add(execution_model("execution-1", external_execution_id="fill-1", source="BROKER_FILL"))
    session.commit()
    factory = sessionmaker(engine, expire_on_commit=False)
    return factory, ExecutionCommissionRepository(factory)


def fee(amount: str = "1.72", *, operation_id: str = "fee-1") -> ExternalOperation:
    return ExternalOperation(
        operation_id=operation_id,
        parent_operation_id="exchange-order-1",
        account_id="account-1",
        operation_type="BROKER_FEE",
        state="EXECUTED",
        occurred_at=NOW + timedelta(minutes=1),
        payment=Money(Decimal(amount).copy_negate(), "RUB"),
        price=None,
        quantity=Decimal(),
        commission=None,
        instrument_id="external-instrument-1",
    )


def test_fee_stays_candidate_until_full_pass_and_accepts_partial_expired_fill(core_database) -> None:
    factory, repository = ready_repository(core_database)
    service = ExecutionCommissionService(repository, now=lambda: NOW + timedelta(minutes=2))
    claim = service.claim_next_page()
    assert claim is not None

    assert service.accept_page(claim, OperationsPage((fee(),), "next")) is True
    with factory() as session:
        candidate = session.scalar(select(BrokerOrderFeeObservationModel))
        state = session.get(BrokerFeeScanStateModel, "scope-1")
        assert candidate.status == "CANDIDATE"
        assert state.cursor == "next"

    next_page = service.claim_next_page()
    assert next_page is not None
    assert next_page.scan_pass_id == claim.scan_pass_id
    assert service.accept_page(next_page, OperationsPage((), None)) is True
    with factory() as session:
        candidate = session.scalar(select(BrokerOrderFeeObservationModel))
        assert candidate.status == "KNOWN"


def test_last_complete_fee_projection_survives_the_next_provisional_pass(core_database) -> None:
    factory, repository = ready_repository(core_database)
    service = ExecutionCommissionService(repository, now=lambda: NOW + timedelta(minutes=2))
    claim = service.claim_next_page()
    assert claim is not None
    assert service.accept_page(claim, OperationsPage((fee(),), None)) is True

    with factory.begin() as session:
        session.add(
            PositionLotModel(
                id="known-fee-lot",
                user_broker_id="scope-1",
                automation_id="automation-1",
                position_cycle_id="cycle-1",
                buy_execution_id="execution-1",
                source="BROKER_EXECUTION",
                original_lots=1,
                remaining_lots=1,
                entry_price=Decimal("10.25"),
                entry_commission=Decimal("0.01"),
                opened_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    automations = AutomationRepository(factory)
    completed = automations.get("automation-1")
    assert completed.financial_status == "COMPLETE"
    assert completed.actual_commissions == Decimal("1.72")

    next_claim = service.claim_next_page()
    assert next_claim is not None
    assert next_claim.scan_pass_id != claim.scan_pass_id

    provisional = automations.get("automation-1")
    assert provisional.financial_status == "COMPLETE"
    assert provisional.actual_commissions == Decimal("1.72")


def test_public_pnl_uses_effective_fees_across_partial_buy_sell_allocation(core_database) -> None:
    factory, _repository = ready_repository(core_database)
    with factory.begin() as session:
        broker = session.get(UserBrokerModel, "scope-1")
        broker.environment = "PROD"
        cycle = session.get_one(PositionCycleModel, "cycle-1")
        cycle.realized_pnl = Decimal("1.46")
        cycle.unrealized_pnl = Decimal("0.30")
        cycle.net_pnl = Decimal("1.76")
        cycle.accumulated_commissions = Decimal("0.05")
        buy_order = session.get_one(BrokerOrderModel, "order-1")
        buy_order.external_order_id = "exchange-buy"
        buy_order.quantity_lots = 3
        buy_order.executed_amount = Decimal("30.75")
        buy_order.executed_commission = Decimal("0.03")
        buy_order.terminal_at = NOW
        buy_execution = session.get_one(TradeExecutionModel, "execution-1")
        buy_execution.executed_lots = 3
        buy_execution.value = Decimal("30.75")
        buy_execution.broker_commission = Decimal("0.03")
        session.add(
            PositionLotModel(
                id="buy-lot",
                user_broker_id="scope-1",
                automation_id="automation-1",
                position_cycle_id="cycle-1",
                buy_execution_id="execution-1",
                source="BROKER_EXECUTION",
                original_lots=3,
                remaining_lots=1,
                entry_price=Decimal("10.25"),
                entry_commission=Decimal("0.03"),
                opened_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        sell_decision = decision_model("sell-decision")
        sell_decision.fact_id = "fact-sell-decision"
        session.add(sell_decision)
        session.flush()
        sell_order = order_model("sell-order", decision_id="sell-decision")
        sell_order.external_order_id = "exchange-sell"
        sell_order.state = "FILLED"
        sell_order.side = "SELL"
        sell_order.intent_kind = "SELL_PART"
        sell_order.quantity_lots = 2
        sell_order.executed_amount = Decimal("22")
        sell_order.executed_commission = Decimal("0.02")
        sell_order.terminal_at = NOW
        session.add(sell_order)
        session.flush()
        sell_execution = execution_model(
            "sell-execution", external_execution_id="sell-fill", source="BROKER_FILL"
        )
        sell_execution.broker_order_id = "sell-order"
        sell_execution.side = "SELL"
        sell_execution.executed_lots = 2
        sell_execution.price = Decimal("11")
        sell_execution.value = Decimal("22")
        sell_execution.broker_commission = Decimal("0.02")
        session.add(sell_execution)
        session.flush()
        session.add(
            ExecutionLotAllocationModel(
                id="sell-allocation",
                user_broker_id="scope-1",
                automation_id="automation-1",
                position_cycle_id="cycle-1",
                sell_execution_id="sell-execution",
                position_lot_id="buy-lot",
                allocated_lots=2,
                entry_value=Decimal("20.50"),
                exit_value=Decimal("22"),
                entry_commission=Decimal("0.02"),
                exit_commission=Decimal("0.02"),
                realized_pnl=Decimal("1.46"),
                allocated_at=NOW,
                created_at=NOW,
            )
        )
        session.flush()
        scan_pass = "fee-pass-effective-pnl"
        session.add(
            BrokerFeeScanStateModel(
                user_broker_id="scope-1",
                scan_pass_id=scan_pass,
                completed_scan_pass_id=scan_pass,
                lower_bound=NOW,
                upper_bound=NOW,
                seen_cursors=[],
                status="COMPLETE",
                completed_at=NOW,
            )
        )
        for order_id, parent_id, amount in (
            ("order-1", "exchange-buy", "1.72"),
            ("sell-order", "exchange-sell", "0.14"),
        ):
            session.add(
                BrokerOrderFeeObservationModel(
                    user_broker_id="scope-1",
                    broker_order_id=order_id,
                    parent_operation_id=parent_id,
                    operation_type="BROKER_FEE",
                    currency="RUB",
                    scan_pass_id=scan_pass,
                    operation_id=f"fee-{order_id}",
                    amount=Decimal(amount),
                    status="KNOWN",
                    occurred_at=NOW,
                    observed_at=NOW,
                )
            )

    record = AutomationRepository(factory).get("automation-1")

    assert record.financial_status == "COMPLETE"
    assert record.realized_pnl == Decimal("0.213333334")
    assert record.unrealized_pnl == Decimal("-0.263333334")
    assert record.net_pnl == Decimal("-0.05")
    assert record.actual_commissions == Decimal("1.86")


@pytest.mark.parametrize("state", ["REJECTED", "EXPIRED"])
def test_terminal_partial_fills_are_eligible_but_failed_orders_are_not(core_database, state: str) -> None:
    factory, repository = ready_repository(core_database)
    with factory.begin() as session:
        session.get(BrokerOrderModel, "order-1").state = state
    service = ExecutionCommissionService(repository, now=lambda: NOW + timedelta(minutes=2))
    claim = service.claim_next_page()
    assert claim is not None
    assert service.accept_page(claim, OperationsPage((fee(),), None)) is True
    with factory() as session:
        observation = session.scalar(select(BrokerOrderFeeObservationModel))
    assert observation.status == "KNOWN"


def test_same_pass_conflicting_amounts_remain_unknown_across_pages(core_database) -> None:
    factory, repository = ready_repository(core_database)
    service = ExecutionCommissionService(repository, now=lambda: NOW + timedelta(minutes=2))
    first = service.claim_next_page()
    assert first is not None
    assert service.accept_page(first, OperationsPage((fee("1.72"),), "next")) is True
    second = service.claim_next_page()
    assert second is not None
    assert service.accept_page(second, OperationsPage((fee("1.75", operation_id="fee-2"),), None)) is True
    with factory() as session:
        observations = session.scalars(select(BrokerOrderFeeObservationModel)).all()
    assert {item.status for item in observations} == {"UNKNOWN"}


def test_replay_with_mutable_operation_id_is_idempotent(core_database) -> None:
    factory, repository = ready_repository(core_database)
    service = ExecutionCommissionService(repository, now=lambda: NOW + timedelta(minutes=2))
    claim = service.claim_next_page()
    assert claim is not None
    assert service.accept_page(
        claim,
        OperationsPage((fee(operation_id="old-id"), fee(operation_id="new-id")), None),
    ) is True
    with factory() as session:
        observations = session.scalars(select(BrokerOrderFeeObservationModel)).all()
    assert len(observations) == 1
    assert sum(item.status == "KNOWN" for item in observations) == 1


def test_stale_page_claim_cannot_advance_cursor(core_database) -> None:
    factory, repository = ready_repository(core_database)
    service = ExecutionCommissionService(repository, now=lambda: NOW + timedelta(minutes=2))
    stale = service.claim_next_page()
    assert stale is not None
    current = service.claim_next_page()
    assert current is not None
    assert service.accept_page(stale, OperationsPage((fee(),), None)) is False
    with factory() as session:
        assert session.scalar(select(BrokerOrderFeeObservationModel)) is None
        assert session.get(BrokerFeeScanStateModel, "scope-1").claim_id == current.claim_id


def test_admin_failed_order_is_recorded_unknown_not_known(core_database) -> None:
    factory, repository = ready_repository(core_database)
    with factory.begin() as session:
        order = session.get(BrokerOrderModel, "order-1")
        order.state = "FAILED"
    service = ExecutionCommissionService(repository, now=lambda: NOW + timedelta(minutes=2))
    claim = service.claim_next_page()
    assert claim is not None
    assert service.accept_page(claim, OperationsPage((fee(),), None)) is True
    with factory() as session:
        observation = session.scalar(select(BrokerOrderFeeObservationModel))
    assert observation.status == "UNKNOWN"


def test_oversized_source_page_is_rejected_before_observations_are_written(core_database) -> None:
    factory, repository = ready_repository(core_database)
    service = ExecutionCommissionService(repository, now=lambda: NOW + timedelta(minutes=2))
    claim = service.claim_next_page()
    assert claim is not None

    assert service.accept_page(claim, OperationsPage(tuple(fee() for _ in range(101)), None)) is False

    with factory() as session:
        assert session.scalar(select(BrokerOrderFeeObservationModel)) is None
        state = session.get(BrokerFeeScanStateModel, "scope-1")
        assert state.status == "ERROR"
        assert state.last_error_code == "SOURCE_PAGE_TOO_LARGE"


def test_mismatched_source_account_is_rejected_before_observation_write(core_database) -> None:
    factory, repository = ready_repository(core_database)
    service = ExecutionCommissionService(repository, now=lambda: NOW + timedelta(minutes=2))
    claim = service.claim_next_page()
    assert claim is not None

    other_account = fee().model_copy(update={"account_id": "other"})
    assert service.accept_page(claim, OperationsPage((other_account,), None)) is False

    with factory() as session:
        assert session.scalar(select(BrokerOrderFeeObservationModel)) is None
        state = session.get(BrokerFeeScanStateModel, "scope-1")
        assert state.last_error_code == "SOURCE_ACCOUNT_MISMATCH"




def test_observation_dto_preserves_exact_semantic_scope(core_database) -> None:
    fee_value = BrokerFeeObservation(
        user_broker_id="scope-1",
        account_id="account-1",
        parent_operation_id="exchange-order-1",
        operation_id="fee-1",
        instrument_id="external-instrument-1",
        currency="rub",
        amount=Decimal("1.72"),
        occurred_at=NOW,
    )
    assert fee_value.currency == "RUB"
    assert fee_value.amount == Decimal("1.72")
