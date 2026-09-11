from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel_contracts.business_audit import (
    BusinessAuditEvent,
    BusinessAuditLevel,
    BusinessAuditStage,
    PublishAuditResult,
)


def test_business_audit_event_preserves_process_context_and_stage() -> None:
    event = BusinessAuditEvent(
        event_id="00000000-0000-0000-0000-000000000001",
        process_id="00000000-0000-0000-0000-000000000002",
        parent_process_id=None,
        automation_id="automation-1",
        broker_id="broker-1",
        account_id="account-1",
        instrument_id="instrument-1",
        level=BusinessAuditLevel.INFO,
        stage=BusinessAuditStage.STRATEGY_DECISION_MADE,
        message="Strategy decision calculated",
        data={"decision": "WAIT", "reason_code": "NO_THRESHOLD"},
        occurred_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        critical=False,
    )

    assert event.process_id == "00000000-0000-0000-0000-000000000002"
    assert event.stage.value == "STRATEGY_DECISION_MADE"
    assert event.level.value == "INFO"

    with pytest.raises(ValidationError, match="Instance is frozen") as raised:
        event.message = "changed"  # type: ignore[misc]
    assert raised.value.errors()[0]["type"] == "frozen_instance"


def test_business_audit_contract_exposes_only_supported_levels() -> None:
    assert {item.value for item in BusinessAuditLevel} == {"INFO", "WARNING", "ERROR"}


def test_publish_result_keeps_unique_accepted_event_ids() -> None:
    result = PublishAuditResult(
        accepted_event_ids=(
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        )
    )

    assert result.accepted_event_ids == (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )
