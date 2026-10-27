from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from moex_sentinel.usecases.execution_diagnostics import ViewExecutionDiagnosticsUsecase

CAPTURED_AT = datetime(2026, 10, 26, 12, tzinfo=UTC)
AUTOMATION_ID = "11111111-1111-4111-8111-111111111111"
BROKER_ID = "22222222-2222-4222-8222-222222222222"
INSTRUMENT_ID = "33333333-3333-4333-8333-333333333333"


class FakeWorkerRow:
    def __init__(self, created_at_values):
        self.intents = tuple(SimpleNamespace(created_at=value) for value in created_at_values)

    def model_dump(self, *, mode):
        assert mode == "json"
        return {"intents": [{"created_at": intent.created_at.isoformat()} for intent in self.intents]}


class FakeUnitOfWork:
    def __enter__(self):
        self.orders = SimpleNamespace(
            execution_diagnostics=lambda *args, **kwargs: {
                "orders": ({"created_at": CAPTURED_AT - timedelta(hours=24, milliseconds=1)},),
                "events": ({"occurred_at": CAPTURED_AT - timedelta(hours=24)},),
                "executions": ({"executed_at": CAPTURED_AT - timedelta(hours=25)},),
                "accepted_envelopes": ({"received_at": CAPTURED_AT},),
                "truncated": False,
            }
        )
        return self

    def __exit__(self, *args):
        return False

    def execution_diagnostics_scope(self, *args, **kwargs):
        return {
            "account_id": "account-1",
            "external_instrument_id": "instrument-1",
            "instrument_id": INSTRUMENT_ID,
            "lot_size": 1,
            "currency": "RUB",
            "owner_runtime_instance_id": None,
            "owner_generation": None,
            "reservation_truncated": False,
            "reservations": ({"created_at": CAPTURED_AT - timedelta(hours=24, milliseconds=1)},),
        }


def test_execution_history_projection_marks_all_record_rows_without_hiding_them() -> None:
    worker_row = FakeWorkerRow(
        [
            CAPTURED_AT - timedelta(hours=24),
            CAPTURED_AT - timedelta(hours=24, milliseconds=1),
        ]
    )
    worker = SimpleNamespace(
        observation="OBSERVED",
        reason="OBSERVED",
        broker_id=UUID(BROKER_ID),
        captured_at=CAPTURED_AT,
        received_at=CAPTURED_AT.isoformat(),
        source_runtime_instance_id=None,
        source_generation=None,
        row=worker_row,
    )
    usecase = ViewExecutionDiagnosticsUsecase(
        SimpleNamespace(execute=lambda _: SimpleNamespace(broker_id=BROKER_ID)),
        FakeUnitOfWork,
        clock=lambda: CAPTURED_AT,
    )

    result = usecase.execute(AUTOMATION_ID, worker_store=SimpleNamespace(execution_snapshot=lambda **_: worker))

    assert [row["historical"] for row in result.orders] == [True]
    assert [row["historical"] for row in result.events] == [False]
    assert [row["historical"] for row in result.executions] == [True]
    assert [row["historical"] for row in result.accepted_envelopes] == [False]
    assert [row["historical"] for row in result.reservations] == [True]
    intents = result.worker_observation["row"]["intents"]
    assert [row["historical"] for row in intents] == [False, True]
    assert len(result.orders) == len(result.events) == len(result.executions) == 1
