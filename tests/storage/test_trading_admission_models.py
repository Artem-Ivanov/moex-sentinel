from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingAdmissionBaselineModel,
    TradingAdmissionEventModel,
    TradingReconciliationModel,
    TradingSubmitReservationModel,
    WorkerRuntimeInstanceModel,
)
from tests.storage.trading_facts_helpers import user_broker_model

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
SCOPE = "00000000-0000-4000-8000-000000000001"
RUNTIME = "00000000-0000-4000-8000-000000000002"


def test_admission_storage_keeps_owner_digest_and_scope_unique(core_engine):
    with Session(core_engine) as session, session.begin():
        session.add(user_broker_model(SCOPE, "synthetic-account"))
        session.flush()
        session.add(
            WorkerRuntimeInstanceModel(
                runtime_instance_id=RUNTIME,
                user_broker_id=SCOPE,
                worker_id="worker-1",
                environment="PROD",
                access_mode="READ_ONLY",
                strategy_enabled=False,
                secret_digest="a" * 64,
                registration_status="OWNER",
                generation=1,
                registered_at=NOW,
                last_seen_at=NOW,
                lease_expires_at=NOW,
            )
        )
        session.flush()
        session.add(
            TradingAccountAdmissionModel(
                user_broker_id=SCOPE,
                revision=1,
                owner_runtime_instance_id=RUNTIME,
                owner_generation=1,
                admission_status="DISARMED",
                recovery_only=False,
            )
        )
        session.flush()
        assert session.scalar(select(TradingAccountAdmissionModel.user_broker_id)) == SCOPE

    with Session(core_engine) as session, session.begin():
        session.add(
            TradingAccountAdmissionModel(
                user_broker_id=SCOPE,
                revision=1,
                admission_status="DISARMED",
                recovery_only=False,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_admission_financial_tables_preserve_baseline_and_idempotency_constraints(core_engine):
    with Session(core_engine) as session, session.begin():
        session.add(user_broker_model(SCOPE, "synthetic-account"))
        session.flush()
        session.add(
            TradingAdmissionBaselineModel(
                baseline_id="00000000-0000-4000-8000-000000000011",
                user_broker_id=SCOPE,
                account_id="synthetic-account",
                moscow_day=date(2026, 10, 9),
                currency="RUB",
                equity=Decimal("10000"),
                net_external_cash_flow=Decimal("0"),
                capture_started_at=NOW,
                capture_finished_at=NOW,
                operations_through=NOW,
                accepted_by="operator",
                accepted_at=NOW,
                payload={"source": "GET_PORTFOLIO", "policy": "FIRST_ACCEPTED_ACCOUNT_EQUITY"},
            )
        )
        session.add(
            TradingReconciliationModel(
                reconciliation_id="00000000-0000-4000-8000-000000000012",
                user_broker_id=SCOPE,
                account_id="synthetic-account",
                scope="ACCOUNT",
                owner_runtime_instance_id=RUNTIME,
                owner_generation=1,
                configuration_fingerprint="b" * 64,
                status="ACCOUNT_ONLY",
                captured_at=NOW,
                valid_until=NOW,
                can_arm=False,
                active_ordinary_order_count=0,
                active_stop_order_count=0,
                unresolved_reservation_count=0,
                reasons=["RECONCILIATION_REQUIRED"],
                payload={},
            )
        )
        session.flush()

        reservation = TradingSubmitReservationModel(
            reservation_id="00000000-0000-4000-8000-000000000013",
            user_broker_id=SCOPE,
            automation_id="00000000-0000-4000-8000-000000000021",
            idempotency_key="00000000-0000-4000-8000-000000000022",
            request_fingerprint="c" * 64,
            state="IN_FLIGHT",
            runtime_instance_id=RUNTIME,
            generation=1,
            configuration_fingerprint="b" * 64,
            side="BUY",
            instrument_uid="TCS@TQBR",
            quantity_lots=1,
            limit_price=Decimal("100"),
            requested_amount=Decimal("100"),
            reserved_cash=Decimal("101"),
            reserved_position_units=Decimal("0"),
            payload={},
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(reservation)
        session.flush()
        assert reservation.state == "IN_FLIGHT"

    with Session(core_engine) as session, session.begin():
        session.add(
            TradingSubmitReservationModel(
                reservation_id="00000000-0000-4000-8000-000000000014",
                user_broker_id=SCOPE,
                automation_id="00000000-0000-4000-8000-000000000021",
                idempotency_key="00000000-0000-4000-8000-000000000022",
                request_fingerprint="c" * 64,
                state="UNKNOWN",
                runtime_instance_id=RUNTIME,
                generation=1,
                configuration_fingerprint="b" * 64,
                side="BUY",
                instrument_uid="TCS@TQBR",
                quantity_lots=1,
                limit_price=Decimal("100"),
                requested_amount=Decimal("100"),
                reserved_cash=Decimal("101"),
                reserved_position_units=Decimal("0"),
                payload={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_admission_events_are_append_only_rows_linked_to_scope(core_engine):
    with Session(core_engine) as session, session.begin():
        session.add(user_broker_model(SCOPE, "synthetic-account"))
        session.flush()
        session.add(
            TradingAdmissionEventModel(
                event_id="00000000-0000-4000-8000-000000000031",
                user_broker_id=SCOPE,
                event_type="RUNTIME_REGISTERED",
                occurred_at=NOW,
                payload={"runtime_instance_id": RUNTIME},
            )
        )
