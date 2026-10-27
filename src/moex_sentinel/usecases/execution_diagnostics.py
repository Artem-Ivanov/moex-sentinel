"""Read bounded Core facts and the currently trusted Worker projection."""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from moex_sentinel.domain.execution_diagnostics import ExecutionDiagnosticsView
from moex_sentinel.services.trading_fact_ports import TradingFactsUnitOfWorkPort
from moex_sentinel.usecases.automations import ViewTradingAutomationUsecase
from moex_sentinel.usecases.errors import UseCaseError
from sentinel_contracts.time import is_historical, utc_now_ms


def _historical_rows(rows: tuple[dict[str, Any], ...], timestamp_key: str, captured_at: datetime):
    return tuple({**row, "historical": is_historical(row[timestamp_key], captured_at)} for row in rows)


class ViewExecutionDiagnosticsUsecase:
    def __init__(
        self,
        automation_lookup: ViewTradingAutomationUsecase,
        uow_factory: Callable[[], TradingFactsUnitOfWorkPort],
        *,
        clock: Callable[[], datetime] = utc_now_ms,
    ) -> None:
        self._automation_lookup = automation_lookup
        self._uow_factory = uow_factory
        self._clock = clock

    def execute(
        self, automation_id: str, *, limit: int = 20, intent_id: UUID | None = None, worker_store: Any
    ) -> ExecutionDiagnosticsView:
        automation = self._automation_lookup.execute(automation_id)
        captured_at = self._clock()
        with self._uow_factory() as uow:
            scope = uow.execution_diagnostics_scope(
                automation.broker_id,
                automation_id,
                limit=limit,
                intent_id=None if intent_id is None else str(intent_id),
            )
            if scope is None:
                raise UseCaseError("AUTOMATION_NOT_FOUND", "Торговый автомат не найден.")
            facts = uow.orders.execution_diagnostics(
                automation.broker_id,
                automation_id,
                limit=limit,
                intent_id=None if intent_id is None else str(intent_id),
            )
        worker = worker_store.execution_snapshot(
            broker_id=UUID(automation.broker_id),
            automation_id=UUID(automation_id),
            account_id=str(scope["account_id"]),
            instrument_id=str(scope["external_instrument_id"]),
            fact_instrument_id=UUID(str(scope["instrument_id"])),
            lot_size=int(scope["lot_size"]),
            owner_runtime_instance_id=(
                None if scope["owner_runtime_instance_id"] is None else UUID(str(scope["owner_runtime_instance_id"]))
            ),
            owner_generation=scope["owner_generation"],
        )
        worker_row = None if worker.row is None else worker.row.model_dump(mode="json")
        if worker.row is not None:
            for row, intent in zip(worker_row["intents"], worker.row.intents, strict=True):
                row["historical"] = is_historical(intent.created_at, captured_at)
        worker_payload = {
            "observation": worker.observation,
            "reason": worker.reason,
            "broker_id": None if worker.broker_id is None else str(worker.broker_id),
            "captured_at": None if worker.captured_at is None else worker.captured_at.isoformat(),
            "received_at": worker.received_at,
            "source_runtime_instance_id": (
                None if worker.source_runtime_instance_id is None else str(worker.source_runtime_instance_id)
            ),
            "source_generation": worker.source_generation,
            "row": worker_row,
        }
        reservations = scope["reservations"]
        return ExecutionDiagnosticsView(
            automation_id=UUID(automation_id),
            broker_id=UUID(automation.broker_id),
            account_id=str(scope["account_id"]),
            instrument_id=UUID(str(scope["instrument_id"])),
            external_instrument_id=str(scope["external_instrument_id"]),
            catalog_lot_size=int(scope["lot_size"]),
            currency=str(scope["currency"]),
            captured_at=captured_at,
            worker_observation=worker_payload,
            owner_runtime_instance_id=(
                None if scope["owner_runtime_instance_id"] is None else UUID(str(scope["owner_runtime_instance_id"]))
            ),
            owner_generation=scope["owner_generation"],
            orders=_historical_rows(facts["orders"], "created_at", captured_at),
            reservations=_historical_rows(reservations, "created_at", captured_at),
            events=_historical_rows(facts["events"], "occurred_at", captured_at),
            executions=_historical_rows(facts["executions"], "executed_at", captured_at),
            accepted_envelopes=_historical_rows(facts["accepted_envelopes"], "received_at", captured_at),
            truncated=bool(scope["reservation_truncated"] or facts["truncated"]),
        )
