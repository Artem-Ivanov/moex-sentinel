from datetime import UTC, datetime

from sentinel_contracts.business_audit import BusinessAuditLevel, BusinessAuditStage
from trading_automaton.services.business_audit import BusinessAuditService


class Repository:
    def __init__(self) -> None:
        self.events = []

    def append_audit_events(self, events):
        self.events.extend(events)


def test_decision_process_audit_uses_one_process_id_and_safe_allowlist() -> None:
    repository = Repository()
    service = BusinessAuditService(repository, now=lambda: datetime(2026, 8, 7, 12, tzinfo=UTC))

    service.record_decision_process(
        process_id="process-1",
        automation_id="automation-1",
        broker_id="broker-1",
        account_id="account-1",
        instrument_id="instrument-1",
        decision="WAIT",
        reason_code="NO_THRESHOLD",
        current_price="100.00",
        best_bid="99.99",
        best_ask="100.01",
        averaging_step_percent="0.50",
        minimum_net_profit_percent="0.50",
        order_book_age_ms=250,
        trading_status="NORMAL_TRADING",
        free_cash="5000",
        reserved_cash="1000",
        required_order_cash="1001",
        estimated_buy_commission="1",
        available_after_reserve="3999",
        cycle_pending_low_before="99.50",
        cycle_pending_low_after="99.00",
        unsafe_value="must-not-be-stored",
    )

    assert {event.process_id for event in repository.events} == {"process-1"}
    assert [event.stage for event in repository.events] == [
        BusinessAuditStage.MARKET_DATA_RECEIVED,
        BusinessAuditStage.TRADING_CYCLE_UPDATED,
        BusinessAuditStage.POSITION_SNAPSHOT_RECEIVED,
        BusinessAuditStage.STRATEGY_CALCULATION_STARTED,
        BusinessAuditStage.STRATEGY_DECISION_MADE,
        BusinessAuditStage.TRADING_STEP_COMPLETED,
    ]
    decision = repository.events[4]
    assert decision.data == {
        "decision": "WAIT",
        "reason_code": "NO_THRESHOLD",
        "current_price": "100.00",
        "best_bid": "99.99",
        "best_ask": "100.01",
        "averaging_step_percent": "0.50",
        "minimum_net_profit_percent": "0.50",
        "order_book_age_ms": 250,
        "trading_status": "NORMAL_TRADING",
        "free_cash": "5000",
        "reserved_cash": "1000",
        "required_order_cash": "1001",
        "estimated_buy_commission": "1",
        "available_after_reserve": "3999",
        "cycle_pending_low_before": "99.50",
        "cycle_pending_low_after": "99.00",
    }


def test_order_failure_audit_is_critical_and_keeps_same_process_id() -> None:
    repository = Repository()
    service = BusinessAuditService(repository, now=lambda: datetime(2026, 8, 7, 12, tzinfo=UTC))

    service.record_order_stage(
        stage=BusinessAuditStage.TRADING_STEP_FAILED,
        process_id="process-1",
        automation_id="automation-1",
        broker_id="broker-1",
        account_id="account-1",
        instrument_id="instrument-1",
        message="Broker dispatch failed",
        critical=True,
        broker_order_state="UNCERTAIN",
        exception_type="TimeoutError",
        broker_error_code="DEADLINE_EXCEEDED",
        broker_error_details="broker response details",
        position_snapshot={"quantity_lots": "2"},
        recent_operations=[{"state": "EXECUTED", "quantity": "1"}],
        unsafe_payload="must-not-be-stored",
    )

    stored = repository.events[0]
    assert stored.process_id == "process-1"
    assert stored.level is BusinessAuditLevel.ERROR
    assert stored.critical is True
    assert stored.data == {
        "broker_order_state": "UNCERTAIN",
        "exception_type": "TimeoutError",
        "broker_error_code": "DEADLINE_EXCEEDED",
        "broker_error_details": "broker response details",
        "position_snapshot": {"quantity_lots": "2"},
        "recent_operations": [{"state": "EXECUTED", "quantity": "1"}],
    }


def test_position_reconciliation_audit_records_compared_quantities() -> None:
    repository = Repository()
    service = BusinessAuditService(repository, now=lambda: datetime(2026, 8, 7, 12, tzinfo=UTC))

    service.record_reconciliation(
        stage=BusinessAuditStage.POSITION_RECONCILED,
        process_id="process-1",
        automation_id="automation-1",
        broker_id="broker-1",
        account_id="account-1",
        instrument_id="instrument-1",
        broker_quantity_lots=2,
        worker_quantity_lots=2,
        reason_code="POSITION_CONSISTENT",
    )

    event = repository.events[0]
    assert event.stage is BusinessAuditStage.POSITION_RECONCILED
    assert event.process_id == "process-1"
    assert event.data == {
        "broker_quantity_lots": 2,
        "worker_quantity_lots": 2,
        "reason_code": "POSITION_CONSISTENT",
    }
