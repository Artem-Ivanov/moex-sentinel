from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from moex_sentinel.domain import daily_admission_preparation as contract
from sentinel_contracts.trading_admission import AdmissionLimits, SubmitDenialReason


def test_daily_preparation_view_exposes_only_safe_operator_fields() -> None:
    view = contract.DailyAdmissionPreparationView(
        moscow_day=date(2026, 10, 14),
        captured_at=datetime(2026, 10, 14, 8, 0, tzinfo=UTC),
        status=contract.DailyAdmissionPreparationStatus.PREPARED,
        reason=contract.DailyAdmissionPreparationReason.ARM_REQUIRED,
        baseline_id=UUID("00000000-0000-4000-8000-000000000001"),
        owner_runtime_instance_id=UUID("00000000-0000-4000-8000-000000000002"),
        owner_generation=7,
        configuration_fingerprint="a" * 64,
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000003"),
        reconciliation_valid_until=datetime(2026, 10, 14, 8, 0, 30, tzinfo=UTC),
        suggested_limits=AdmissionLimits(
            currency="RUB",
            max_order_notional=Decimal("10000.00"),
            exposure_scope="ACCOUNT_GROSS",
            max_exposure=Decimal("10000.00"),
            max_outstanding_orders=1,
            max_daily_loss=Decimal("500.00"),
        ),
        risk_reasons=(),
    )

    assert view.model_dump(mode="json") == {
        "moscow_day": "2026-10-14",
        "captured_at": "2026-10-14T08:00:00.000Z",
        "status": "PREPARED",
        "reason": "ARM_REQUIRED",
        "baseline_id": "00000000-0000-4000-8000-000000000001",
        "owner_runtime_instance_id": "00000000-0000-4000-8000-000000000002",
        "owner_generation": 7,
        "configuration_fingerprint": "a" * 64,
        "reconciliation_id": "00000000-0000-4000-8000-000000000003",
        "reconciliation_valid_until": "2026-10-14T08:00:30.000Z",
        "suggested_limits": {
            "currency": "RUB",
            "max_order_notional": "10000.00",
            "exposure_scope": "ACCOUNT_GROSS",
            "max_exposure": "10000.00",
            "max_outstanding_orders": 1,
            "max_daily_loss": "500.00",
        },
        "risk_reasons": [],
    }
    assert "account_id" not in view.model_dump(mode="json")


def test_daily_preparation_reason_rejects_untrusted_sdk_text() -> None:
    with pytest.raises(ValidationError):
        contract.DailyAdmissionPreparationView(
            moscow_day=date(2026, 10, 14),
            captured_at=datetime(2026, 10, 14, 8, 0, tzinfo=UTC),
            status=contract.DailyAdmissionPreparationStatus.BLOCKED,
            reason="token=do-not-return-this",
            baseline_id=None,
            owner_runtime_instance_id=None,
            owner_generation=None,
            configuration_fingerprint=None,
            reconciliation_id=None,
            reconciliation_valid_until=None,
            suggested_limits=None,
            risk_reasons=(SubmitDenialReason.CORE_UNAVAILABLE,),
        )
