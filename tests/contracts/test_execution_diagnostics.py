from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from sentinel_contracts.worker_diagnostics import (
    WorkerAutomationExecutionDiagnostics,
    WorkerDiagnostics,
    WorkerExecutionDiagnostics,
    WorkerIntentDiagnostics,
)

NOW = datetime(2026, 10, 20, 12, 0, 0, 123456, tzinfo=UTC)
AUTOMATION_ID = UUID("00000000-0000-4000-8000-000000000101")
BROKER_ID = UUID("00000000-0000-4000-8000-000000000102")
INTENT_ID = UUID("00000000-0000-4000-8000-000000000103")


def intent(*, intent_id: UUID = INTENT_ID) -> WorkerIntentDiagnostics:
    return WorkerIntentDiagnostics(
        intent_id=intent_id,
        decision_id=UUID("00000000-0000-4000-8000-000000000104"),
        process_id=None,
        active=True,
        state="UNCERTAIN",
        side="BUY",
        external_order_id=None,
        quantity_lots=1,
        limit_price=Decimal("10.5"),
        requested_amount=Decimal("10.5"),
        executed_lots=0,
        executed_amount=Decimal("0"),
        executed_price=Decimal("0"),
        estimated_commission=Decimal("0.1"),
        executed_commission=Decimal("0"),
        currency="RUB",
        fact_execution_id=None,
        execution_facts_emitted_at=None,
        origin_runtime_instance_id=None,
        origin_generation=None,
        created_at=NOW,
        updated_at=NOW,
        dispatch_started_at=None,
        broker_responded_at=None,
        executed_at=None,
        terminal_at=None,
    )


def test_execution_projection_is_bounded_and_normalizes_timestamps() -> None:
    row = WorkerAutomationExecutionDiagnostics(
        automation_id=AUTOMATION_ID,
        account_id="account-1",
        external_instrument_id="instrument-1",
        fact_instrument_id=UUID("00000000-0000-4000-8000-000000000105"),
        lot_size=10,
        pending_fact_count=2,
        failed_fact_count=0,
        intents=(intent(),),
        history_truncated=False,
    )
    projection = WorkerExecutionDiagnostics(
        observation="OBSERVED",
        reason="OBSERVED",
        broker_id=BROKER_ID,
        captured_at=NOW,
        rows=(row,),
        truncated=False,
    )

    assert projection.captured_at == NOW.replace(microsecond=123000)
    assert projection.rows[0].intents[0].created_at == NOW.replace(microsecond=123000)
    assert "strategy_snapshot" not in projection.model_dump(mode="json")
    assert "safe_message" not in projection.model_dump(mode="json")


def test_unknown_projection_carries_no_financial_rows() -> None:
    projection = WorkerExecutionDiagnostics(
        observation="UNKNOWN",
        reason="BUDGET_EXCEEDED",
        broker_id=BROKER_ID,
        rows=(),
    )

    assert projection.captured_at is None
    assert projection.rows == ()
    with pytest.raises(ValidationError):
        WorkerExecutionDiagnostics(
            observation="UNKNOWN",
            reason="READ_FAILED",
            rows=(
                WorkerAutomationExecutionDiagnostics(
                    automation_id=AUTOMATION_ID,
                    account_id="account-1",
                    external_instrument_id="instrument-1",
                    fact_instrument_id=None,
                    lot_size=1,
                    pending_fact_count=0,
                    failed_fact_count=0,
                ),
            ),
        )


def test_worker_diagnostics_keeps_execution_projection_optional() -> None:
    base = {
        "completed_iterations": 0,
        "last_completed_at": None,
        "last_finished_at": NOW,
        "last_result": "ERROR",
        "error_code": "ITERATION_FAILED",
        "outbox": {"reason": "READ_FAILED"},
    }

    assert WorkerDiagnostics.model_validate(base).execution_diagnostics is None
    assert (
        WorkerDiagnostics.model_validate(
            {**base, "execution_diagnostics": {"observation": "UNKNOWN", "reason": "READ_FAILED"}}
        ).execution_diagnostics.reason
        == "READ_FAILED"
    )


def test_execution_projection_rejects_sixth_intent_and_hides_raw_fields() -> None:
    with pytest.raises(ValidationError):
        WorkerAutomationExecutionDiagnostics(
            automation_id=AUTOMATION_ID,
            account_id="account-1",
            external_instrument_id="instrument-1",
            fact_instrument_id=None,
            lot_size=1,
            pending_fact_count=0,
            failed_fact_count=0,
            intents=tuple(intent(intent_id=UUID(int=index + 1)) for index in range(6)),
            history_truncated=True,
            raw_payload={"secret": "must-not-pass"},
        )
