# D1 core: final delta from pre-implementation dirty baseline
Source/test hashes: sibling final-hashes.json. Prior dirty work must be preserved. Author report: task-1-report.md. Full backend suite with isolated PostgreSQL is running; no final test claim yet.

## src/moex_sentinel/domain/execution_diagnostics.py

```diff
--- baseline/src/moex_sentinel/domain/execution_diagnostics.py
+++ src/moex_sentinel/domain/execution_diagnostics.py
@@ -0,0 +1,31 @@
+"""Bounded, scoped execution-history read model."""
+
+from datetime import datetime
+from typing import Any
+from uuid import UUID
+
+from pydantic import ConfigDict
+
+from sentinel_contracts.base import PositionalModel
+
+
+class ExecutionDiagnosticsView(PositionalModel):
+    model_config = ConfigDict(frozen=True)
+
+    automation_id: UUID
+    broker_id: UUID
+    account_id: str
+    instrument_id: UUID
+    external_instrument_id: str
+    catalog_lot_size: int
+    currency: str
+    captured_at: datetime
+    worker_observation: dict[str, Any]
+    owner_runtime_instance_id: UUID | None
+    owner_generation: int | None
+    orders: tuple[dict[str, Any], ...]
+    reservations: tuple[dict[str, Any], ...]
+    events: tuple[dict[str, Any], ...]
+    executions: tuple[dict[str, Any], ...]
+    accepted_envelopes: tuple[dict[str, Any], ...]
+    truncated: bool
```

## src/moex_sentinel/usecases/execution_diagnostics.py

```diff
--- baseline/src/moex_sentinel/usecases/execution_diagnostics.py
+++ src/moex_sentinel/usecases/execution_diagnostics.py
@@ -0,0 +1,92 @@
+"""Read bounded Core facts and the currently trusted Worker projection."""
+
+from collections.abc import Callable
+from datetime import datetime
+from typing import Any
+from uuid import UUID
+
+from moex_sentinel.domain.execution_diagnostics import ExecutionDiagnosticsView
+from moex_sentinel.services.trading_fact_ports import TradingFactsUnitOfWorkPort
+from moex_sentinel.usecases.automations import ViewTradingAutomationUsecase
+from moex_sentinel.usecases.errors import UseCaseError
+from sentinel_contracts.time import utc_now_ms
+
+
+class ViewExecutionDiagnosticsUsecase:
+    def __init__(
+        self,
+        automation_lookup: ViewTradingAutomationUsecase,
+        uow_factory: Callable[[], TradingFactsUnitOfWorkPort],
+        *,
+        clock: Callable[[], datetime] = utc_now_ms,
+    ) -> None:
+        self._automation_lookup = automation_lookup
+        self._uow_factory = uow_factory
+        self._clock = clock
+
+    def execute(
+        self, automation_id: str, *, limit: int = 20, intent_id: UUID | None = None, worker_store: Any
+    ) -> ExecutionDiagnosticsView:
+        automation = self._automation_lookup.execute(automation_id)
+        captured_at = self._clock()
+        with self._uow_factory() as uow:
+            scope = uow.execution_diagnostics_scope(
+                automation.broker_id,
+                automation_id,
+                limit=limit,
+                intent_id=None if intent_id is None else str(intent_id),
+            )
+            if scope is None:
+                raise UseCaseError("AUTOMATION_NOT_FOUND", "Торговый автомат не найден.")
+            facts = uow.orders.execution_diagnostics(
+                automation.broker_id,
+                automation_id,
+                limit=limit,
+                intent_id=None if intent_id is None else str(intent_id),
+            )
+        worker = worker_store.execution_snapshot(
+            broker_id=UUID(automation.broker_id),
+            automation_id=UUID(automation_id),
+            account_id=str(scope["account_id"]),
+            instrument_id=str(scope["external_instrument_id"]),
+            fact_instrument_id=UUID(str(scope["instrument_id"])),
+            lot_size=int(scope["lot_size"]),
+            owner_runtime_instance_id=(
+                None if scope["owner_runtime_instance_id"] is None else UUID(str(scope["owner_runtime_instance_id"]))
+            ),
+            owner_generation=scope["owner_generation"],
+        )
+        worker_payload = {
+            "observation": worker.observation,
+            "reason": worker.reason,
+            "broker_id": None if worker.broker_id is None else str(worker.broker_id),
+            "captured_at": None if worker.captured_at is None else worker.captured_at.isoformat(),
+            "received_at": worker.received_at,
+            "source_runtime_instance_id": (
+                None if worker.source_runtime_instance_id is None else str(worker.source_runtime_instance_id)
+            ),
+            "source_generation": worker.source_generation,
+            "row": None if worker.row is None else worker.row.model_dump(mode="json"),
+        }
+        reservations = scope["reservations"]
+        return ExecutionDiagnosticsView(
+            automation_id=UUID(automation_id),
+            broker_id=UUID(automation.broker_id),
+            account_id=str(scope["account_id"]),
+            instrument_id=UUID(str(scope["instrument_id"])),
+            external_instrument_id=str(scope["external_instrument_id"]),
+            catalog_lot_size=int(scope["lot_size"]),
+            currency=str(scope["currency"]),
+            captured_at=captured_at,
+            worker_observation=worker_payload,
+            owner_runtime_instance_id=(
+                None if scope["owner_runtime_instance_id"] is None else UUID(str(scope["owner_runtime_instance_id"]))
+            ),
+            owner_generation=scope["owner_generation"],
+            orders=facts["orders"],
+            reservations=reservations,
+            events=facts["events"],
+            executions=facts["executions"],
+            accepted_envelopes=facts["accepted_envelopes"],
+            truncated=bool(scope["reservation_truncated"] or facts["truncated"]),
+        )
```

## src/moex_sentinel/views/schemas/execution_diagnostics.py

```diff
--- baseline/src/moex_sentinel/views/schemas/execution_diagnostics.py
+++ src/moex_sentinel/views/schemas/execution_diagnostics.py
@@ -0,0 +1,55 @@
+"""HTTP schema for scoped execution diagnostics."""
+
+from datetime import datetime
+from typing import Any
+
+from pydantic import ConfigDict
+
+from moex_sentinel.domain.execution_diagnostics import ExecutionDiagnosticsView
+from sentinel_contracts.base import PositionalModel
+
+
+class ExecutionDiagnosticsSchema(PositionalModel):
+    model_config = ConfigDict(frozen=True)
+
+    automation_id: str
+    broker_id: str
+    account_id: str
+    instrument_id: str
+    external_instrument_id: str
+    catalog_lot_size: int
+    currency: str
+    captured_at: datetime
+    worker_observation: dict[str, Any]
+    owner_runtime_instance_id: str | None
+    owner_generation: int | None
+    orders: tuple[dict[str, Any], ...]
+    reservations: tuple[dict[str, Any], ...]
+    events: tuple[dict[str, Any], ...]
+    executions: tuple[dict[str, Any], ...]
+    accepted_envelopes: tuple[dict[str, Any], ...]
+    truncated: bool
+
+    @classmethod
+    def from_domain(cls, value: ExecutionDiagnosticsView) -> "ExecutionDiagnosticsSchema":
+        return cls(
+            automation_id=str(value.automation_id),
+            broker_id=str(value.broker_id),
+            account_id=value.account_id,
+            instrument_id=str(value.instrument_id),
+            external_instrument_id=value.external_instrument_id,
+            catalog_lot_size=value.catalog_lot_size,
+            currency=value.currency,
+            captured_at=value.captured_at,
+            worker_observation=value.worker_observation,
+            owner_runtime_instance_id=(
+                None if value.owner_runtime_instance_id is None else str(value.owner_runtime_instance_id)
+            ),
+            owner_generation=value.owner_generation,
+            orders=value.orders,
+            reservations=value.reservations,
+            events=value.events,
+            executions=value.executions,
+            accepted_envelopes=value.accepted_envelopes,
+            truncated=value.truncated,
+        )
```

## src/moex_sentinel/views/automations.py

```diff
--- baseline/src/moex_sentinel/views/automations.py
+++ src/moex_sentinel/views/automations.py
@@ -1,8 +1,9 @@
 """Thin HTTP views for trading automation user actions."""
 
 from functools import partial
+from uuid import UUID
 
-from fastapi import APIRouter, Query, Request, status
+from fastapi import APIRouter, Query, Request, Response, status
 
 from moex_sentinel.services.sync_execution import run_sync
 from moex_sentinel.views.dependencies import usecases as _usecases
@@ -15,6 +16,7 @@
     TradingSessionsStatusSchema,
     UpdateAutomationSettingsSchema,
 )
+from moex_sentinel.views.schemas.execution_diagnostics import ExecutionDiagnosticsSchema
 
 router = APIRouter(tags=["trading-automations"])
 
@@ -65,6 +67,30 @@
     return TradingAutomationDetailsSchema.from_domain(
         await _usecases(request).view_trading_automation_details.execute(automation_id)
     )
+
+
+@router.get(
+    "/trading-automations/{automation_id}/execution-diagnostics",
+    response_model=ExecutionDiagnosticsSchema,
+)
+async def view_execution_diagnostics(
+    automation_id: str,
+    request: Request,
+    response: Response,
+    limit: int = Query(default=20, ge=1, le=20),
+    intent_id: UUID | None = None,
+) -> ExecutionDiagnosticsSchema:
+    result = await run_sync(
+        partial(
+            _usecases(request).view_execution_diagnostics.execute,
+            automation_id,
+            limit=limit,
+            intent_id=intent_id,
+            worker_store=request.app.state.worker_versions,
+        )
+    )
+    response.headers["Cache-Control"] = "no-store"
+    return ExecutionDiagnosticsSchema.from_domain(result)
 
 
 @router.get(
```

## src/moex_sentinel/composition.py

```diff
--- baseline/src/moex_sentinel/composition.py
+++ src/moex_sentinel/composition.py
@@ -73,6 +73,7 @@
     ViewBrokerSettingsUsecase,
 )
 from moex_sentinel.usecases.connections import CheckBrokerConnectionUsecase
+from moex_sentinel.usecases.execution_diagnostics import ViewExecutionDiagnosticsUsecase
 from moex_sentinel.usecases.instruments import (
     SetInstrumentSelectionUsecase,
     SynchronizeBrokerInstrumentsUsecase,
@@ -111,6 +112,7 @@
     create_trading_automation: CreateTradingAutomationUsecase
     update_trading_automation_settings: UpdateTradingAutomationSettingsUsecase
     view_trading_automation: ViewTradingAutomationUsecase
+    view_execution_diagnostics: ViewExecutionDiagnosticsUsecase
     view_trading_automation_decision_reasons: ViewTradingAutomationDecisionReasonsUsecase
     view_trading_automation_statuses: ViewTradingAutomationStatusesUsecase
     view_trading_automation_details: ViewTradingAutomationDetailsUsecase
@@ -300,6 +302,9 @@
         create_trading_automation=CreateTradingAutomationUsecase(automation_service),
         update_trading_automation_settings=UpdateTradingAutomationSettingsUsecase(automation_service),
         view_trading_automation=view_trading_automation,
+        view_execution_diagnostics=ViewExecutionDiagnosticsUsecase(
+            view_trading_automation, lambda: TradingFactsUnitOfWork(factory)
+        ),
         view_trading_automation_decision_reasons=ViewTradingAutomationDecisionReasonsUsecase(
             view_trading_automation,
             lambda: TradingFactsUnitOfWork(factory),
```

## src/moex_sentinel/storage/repositories/order_facts.py

```diff
--- baseline/src/moex_sentinel/storage/repositories/order_facts.py
+++ src/moex_sentinel/storage/repositories/order_facts.py
@@ -23,6 +23,7 @@
     TradeExecutionModel,
 )
 from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
+from moex_sentinel.storage.models.trading_observability import AutomationEventModel
 from moex_sentinel.storage.repositories.trading_facts_support import (
     append_idempotent,
     flush_or_translate,
@@ -416,6 +417,182 @@
             raise TradingFactPersistenceError(TradingFactErrorCode.NOT_FOUND, entity_type="broker_order")
         return _order_value(model)
 
+    def execution_diagnostics(
+        self, user_broker_id: str, automation_id: str, *, limit: int, intent_id: str | None = None
+    ):
+        """Read a bounded scalar projection; deliberately does not acquire row locks."""
+        orders = self._session.execute(
+            select(
+                BrokerOrderModel.idempotency_key,
+                BrokerOrderModel.fact_id,
+                BrokerOrderModel.id,
+                BrokerOrderModel.decision_id,
+                TradeDecisionModel.process_id,
+                TradeDecisionModel.decision,
+                TradeDecisionModel.reason_code,
+                TradeDecisionModel.lot_size,
+                BrokerOrderModel.external_order_id,
+                BrokerOrderModel.state,
+                BrokerOrderModel.side,
+                BrokerOrderModel.quantity_lots,
+                BrokerOrderModel.limit_price,
+                BrokerOrderModel.requested_amount,
+                BrokerOrderModel.executed_amount,
+                BrokerOrderModel.estimated_commission,
+                BrokerOrderModel.executed_commission,
+                BrokerOrderModel.created_at,
+                BrokerOrderModel.dispatch_started_at,
+                BrokerOrderModel.broker_responded_at,
+                BrokerOrderModel.executed_at,
+                BrokerOrderModel.terminal_at,
+            )
+            .where(
+                BrokerOrderModel.user_broker_id == user_broker_id,
+                BrokerOrderModel.automation_id == automation_id,
+                *(() if intent_id is None else (BrokerOrderModel.idempotency_key == intent_id,)),
+            )
+            .outerjoin(
+                TradeDecisionModel,
+                (TradeDecisionModel.user_broker_id == BrokerOrderModel.user_broker_id)
+                & (TradeDecisionModel.id == BrokerOrderModel.decision_id),
+            )
+            .order_by(BrokerOrderModel.created_at.desc(), BrokerOrderModel.id)
+            .limit(limit + 1)
+        ).all()
+        truncated = len(orders) > limit
+        orders = orders[:limit]
+        order_ids = [row.id for row in orders]
+        facts = [row.fact_id for row in orders]
+        events = []
+        executions = []
+        envelopes = []
+        if order_ids:
+            events = self._session.execute(
+                select(
+                    BrokerOrderEventModel.id,
+                    BrokerOrderEventModel.fact_id,
+                    BrokerOrderEventModel.broker_order_id,
+                    BrokerOrderEventModel.to_state,
+                    BrokerOrderEventModel.safe_reason,
+                    BrokerOrderEventModel.occurred_at,
+                )
+                .where(
+                    BrokerOrderEventModel.user_broker_id == user_broker_id,
+                    BrokerOrderEventModel.automation_id == automation_id,
+                    BrokerOrderEventModel.broker_order_id.in_(order_ids),
+                )
+                .order_by(BrokerOrderEventModel.occurred_at.desc(), BrokerOrderEventModel.id)
+                .limit(limit + 1)
+            ).all()
+            executions = self._session.execute(
+                select(
+                    TradeExecutionModel.id,
+                    TradeExecutionModel.fact_id,
+                    TradeExecutionModel.broker_order_id,
+                    TradeExecutionModel.external_execution_id,
+                    TradeExecutionModel.side,
+                    TradeExecutionModel.executed_lots,
+                    TradeExecutionModel.price,
+                    TradeExecutionModel.value,
+                    TradeExecutionModel.broker_commission,
+                    TradeExecutionModel.other_fees,
+                    TradeExecutionModel.currency,
+                    TradeExecutionModel.executed_at,
+                )
+                .where(
+                    TradeExecutionModel.user_broker_id == user_broker_id,
+                    TradeExecutionModel.automation_id == automation_id,
+                    TradeExecutionModel.broker_order_id.in_(order_ids),
+                )
+                .order_by(TradeExecutionModel.executed_at.desc(), TradeExecutionModel.id)
+                .limit(limit + 1)
+            ).all()
+            facts.extend(row.fact_id for row in events[:limit])
+            facts.extend(row.fact_id for row in executions[:limit])
+            envelopes = self._session.execute(
+                select(
+                    AutomationEventModel.event_id,
+                    AutomationEventModel.sequence_number,
+                    AutomationEventModel.fact_kind,
+                    AutomationEventModel.received_at,
+                )
+                .where(
+                    AutomationEventModel.user_broker_id == user_broker_id,
+                    AutomationEventModel.automation_id == automation_id,
+                    AutomationEventModel.event_id.in_(facts),
+                )
+                .order_by(AutomationEventModel.sequence_number)
+                .limit(3 * limit)
+            ).all()
+        return {
+            "truncated": truncated or len(events) > limit or len(executions) > limit or len(envelopes) > limit,
+            "orders": tuple(
+                {
+                    "intent_id": row.idempotency_key,
+                    "fact_id": row.fact_id,
+                    "order_id": row.id,
+                    "decision_id": row.decision_id,
+                    "process_id": row.process_id,
+                    "decision": row.decision,
+                    "reason_code": row.reason_code,
+                    "lot_size": row.lot_size,
+                    "external_order_id": row.external_order_id,
+                    "state": row.state,
+                    "side": row.side,
+                    "quantity_lots": row.quantity_lots,
+                    "limit_price": row.limit_price,
+                    "requested_amount": row.requested_amount,
+                    "executed_amount": row.executed_amount,
+                    "estimated_commission": row.estimated_commission,
+                    "executed_commission": row.executed_commission,
+                    "created_at": row.created_at,
+                    "dispatch_started_at": row.dispatch_started_at,
+                    "broker_responded_at": row.broker_responded_at,
+                    "executed_at": row.executed_at,
+                    "terminal_at": row.terminal_at,
+                }
+                for row in orders
+            ),
+            "events": tuple(
+                {
+                    "event_id": row.id,
+                    "fact_id": row.fact_id,
+                    "order_id": row.broker_order_id,
+                    "to_state": row.to_state,
+                    "safe_reason": row.safe_reason,
+                    "occurred_at": row.occurred_at,
+                }
+                for row in events[:limit]
+            ),
+            "executions": tuple(
+                {
+                    "execution_id": row.id,
+                    "fact_id": row.fact_id,
+                    "order_id": row.broker_order_id,
+                    "external_execution_id": row.external_execution_id,
+                    "side": row.side,
+                    "executed_lots": row.executed_lots,
+                    "price": row.price,
+                    "value": row.value,
+                    "broker_commission": row.broker_commission,
+                    "other_fees": row.other_fees,
+                    "currency": row.currency,
+                    "executed_at": row.executed_at,
+                }
+                for row in executions[:limit]
+            ),
+            "accepted_envelopes": tuple(
+                {
+                    "fact_id": row.event_id,
+                    "event_id": row.event_id,
+                    "sequence_number": row.sequence_number,
+                    "fact_kind": row.fact_kind,
+                    "received_at": row.received_at,
+                }
+                for row in envelopes[: 3 * limit]
+            ),
+        }
+
     def list_order_events(self, user_broker_id: str, order_id: str) -> tuple[BrokerOrderEventDraft, ...]:
         statement = (
             select(BrokerOrderEventModel)
```

## src/moex_sentinel/storage/repositories/trading_facts_uow.py

```diff
--- baseline/src/moex_sentinel/storage/repositories/trading_facts_uow.py
+++ src/moex_sentinel/storage/repositories/trading_facts_uow.py
@@ -6,6 +6,8 @@
 from sqlalchemy.orm import Session, sessionmaker
 
 from moex_sentinel.domain.trading_facts import FactRuntimeAuthorization, FactSubmitReservation
+from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
+from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
 from moex_sentinel.storage.models.trading_admission import (
     TradingAccountAdmissionModel,
     TradingSubmitReservationModel,
@@ -123,6 +125,87 @@
             payload=dict(reservation.payload),
             origin_registration_status=None if origin is None else origin.registration_status,
         )
+
+    def execution_diagnostics_scope(
+        self, user_broker_id: str, automation_id: str, *, limit: int, intent_id: str | None = None
+    ) -> dict[str, object] | None:
+        """Read owner, instrument and reservations for a scoped diagnostics response without locks."""
+        if self._session is None:
+            raise RuntimeError("Fact unit of work is not active.")
+        automation = self._session.execute(
+            select(
+                TradingAutomationModel.id,
+                TradingAutomationModel.instrument_id,
+                UserBrokerModel.external_account_id.label("account_id"),
+                BrokerInstrumentModel.external_instrument_id,
+                BrokerInstrumentModel.lot_size,
+                BrokerInstrumentModel.currency,
+            )
+            .join(
+                BrokerInstrumentModel,
+                (BrokerInstrumentModel.id == TradingAutomationModel.instrument_id)
+                & (BrokerInstrumentModel.user_broker_id == TradingAutomationModel.user_broker_id),
+            )
+            .join(UserBrokerModel, UserBrokerModel.id == TradingAutomationModel.user_broker_id)
+            .where(
+                TradingAutomationModel.user_broker_id == user_broker_id,
+                TradingAutomationModel.id == automation_id,
+            )
+        ).one_or_none()
+        if automation is None:
+            return None
+        owner = self._session.execute(
+            select(
+                TradingAccountAdmissionModel.owner_runtime_instance_id,
+                TradingAccountAdmissionModel.owner_generation,
+            ).where(TradingAccountAdmissionModel.user_broker_id == user_broker_id)
+        ).one_or_none()
+        reservation_query = select(
+            TradingSubmitReservationModel.idempotency_key,
+            TradingSubmitReservationModel.state,
+            TradingSubmitReservationModel.runtime_instance_id,
+            TradingSubmitReservationModel.generation,
+            TradingSubmitReservationModel.side,
+            TradingSubmitReservationModel.quantity_lots,
+            TradingSubmitReservationModel.reserved_cash,
+            TradingSubmitReservationModel.reserved_position_units,
+            TradingSubmitReservationModel.created_at,
+            TradingSubmitReservationModel.updated_at,
+        ).where(
+            TradingSubmitReservationModel.user_broker_id == user_broker_id,
+            TradingSubmitReservationModel.automation_id == automation_id,
+        )
+        if intent_id is not None:
+            reservation_query = reservation_query.where(TradingSubmitReservationModel.idempotency_key == intent_id)
+        reservations = self._session.execute(
+            reservation_query.order_by(TradingSubmitReservationModel.updated_at.desc()).limit(limit + 1)
+        ).all()
+        return {
+            "automation_id": automation.id,
+            "instrument_id": automation.instrument_id,
+            "account_id": automation.account_id,
+            "external_instrument_id": automation.external_instrument_id,
+            "lot_size": automation.lot_size,
+            "currency": automation.currency,
+            "owner_runtime_instance_id": None if owner is None else owner.owner_runtime_instance_id,
+            "owner_generation": None if owner is None else owner.owner_generation,
+            "reservation_truncated": len(reservations) > limit,
+            "reservations": tuple(
+                {
+                    "intent_id": row.idempotency_key,
+                    "state": row.state,
+                    "side": row.side,
+                    "origin_runtime_instance_id": row.runtime_instance_id,
+                    "origin_generation": row.generation,
+                    "quantity_lots": row.quantity_lots,
+                    "reserved_cash": row.reserved_cash,
+                    "reserved_position_units": row.reserved_position_units,
+                    "created_at": row.created_at,
+                    "updated_at": row.updated_at,
+                }
+                for row in reservations[:limit]
+            ),
+        }
 
     def confirm_submit_not_sent(self, user_broker_id: str, idempotency_key: str, *, now) -> bool:
         if self._session is None:
```

## src/moex_sentinel/services/trading_fact_ports.py

```diff
--- baseline/src/moex_sentinel/services/trading_fact_ports.py
+++ src/moex_sentinel/services/trading_fact_ports.py
@@ -88,6 +88,10 @@
 
     def list_executions(self, user_broker_id: str, order_id: str) -> tuple[TradeExecutionDraft, ...]: ...
 
+    def execution_diagnostics(
+        self, user_broker_id: str, automation_id: str, *, limit: int, intent_id: str | None = None
+    ) -> dict[str, object]: ...
+
 
 class PositionLedgerPort(Protocol):
     def open_cycle(self, user_broker_id: str, value: PositionCycleDraft) -> PositionCycleDraft: ...
@@ -162,6 +166,10 @@
 class TradingFactsUnitOfWorkPort(Protocol):
     def scope_matches_environment(self, user_broker_id: str, environment: str) -> bool: ...
 
+    def execution_diagnostics_scope(
+        self, user_broker_id: str, automation_id: str, *, limit: int, intent_id: str | None = None
+    ) -> dict[str, object] | None: ...
+
     def authorize_fact_runtime(
         self,
         user_broker_id: str,
```

## tests/storage/test_execution_diagnostics.py

```diff
--- baseline/tests/storage/test_execution_diagnostics.py
+++ tests/storage/test_execution_diagnostics.py
@@ -0,0 +1,167 @@
+from decimal import Decimal
+
+from sqlalchemy import create_engine, event, select
+from sqlalchemy.orm import sessionmaker
+
+from moex_sentinel.storage.models import Base
+from moex_sentinel.storage.models.order_facts import BrokerOrderEventModel, BrokerOrderModel
+from moex_sentinel.storage.models.trading_admission import TradingSubmitReservationModel
+from moex_sentinel.storage.models.trading_observability import AutomationEventModel
+from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
+from tests.storage.trading_facts_helpers import NOW, execution_model, seed_order
+
+
+def test_execution_diagnostics_joins_each_fact_to_exact_accepted_envelope_read_only(tmp_path):
+    engine = create_engine(f"sqlite:///{tmp_path / 'execution-read.db'}")
+    Base.metadata.create_all(engine)
+    factory = sessionmaker(engine, expire_on_commit=False)
+    with factory.begin() as session:
+        seed_order(session)
+        session.scalar(select(BrokerOrderModel)).fact_id = "order-envelope"
+        execution = execution_model("execution-1", external_execution_id="fill-1", source="BROKER_FILL")
+        execution.fact_id = "execution-envelope"
+        session.add(execution)
+        second_execution = execution_model("execution-2", external_execution_id="fill-2", source="BROKER_FILL")
+        second_execution.fact_id = "truncated-execution-envelope"
+        session.add(second_execution)
+        session.add(
+            TradingSubmitReservationModel(
+                reservation_id="reservation-1",
+                user_broker_id="scope-1",
+                automation_id="automation-1",
+                idempotency_key="intent-1",
+                request_fingerprint="a" * 64,
+                state="UNKNOWN",
+                runtime_instance_id="runtime-1",
+                generation=2,
+                configuration_fingerprint="b" * 64,
+                side="BUY",
+                instrument_uid="TEST@TQBR",
+                quantity_lots=3,
+                limit_price=Decimal("10"),
+                requested_amount=Decimal("30"),
+                reserved_cash=Decimal("30"),
+                reserved_position_units=Decimal("0"),
+                payload={"private": "not selected"},
+                created_at=NOW,
+                updated_at=NOW,
+            )
+        )
+        session.add_all(
+            [
+                AutomationEventModel(
+                    event_id="order-envelope",
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=4,
+                    expected_revision=1,
+                    fact_kind="BROKER_ORDER_RECORDED",
+                    safe_message="order accepted",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                ),
+                AutomationEventModel(
+                    event_id="execution-envelope",
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=9,
+                    expected_revision=1,
+                    fact_kind="TRADE_EXECUTION_RECORDED",
+                    safe_message="execution accepted",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                ),
+                AutomationEventModel(
+                    event_id="state-envelope",
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=6,
+                    expected_revision=1,
+                    fact_kind="BROKER_ORDER_STATE_CHANGED",
+                    safe_message="state accepted",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                ),
+                AutomationEventModel(
+                    event_id="unlinked-latest-envelope",
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=99,
+                    expected_revision=1,
+                    fact_kind="TRADE_EXECUTION_RECORDED",
+                    safe_message="must not look like an acknowledgement",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                ),
+            ]
+        )
+        session.add(
+            BrokerOrderEventModel(
+                id="order-event-1",
+                fact_id="state-envelope",
+                user_broker_id="scope-1",
+                automation_id="automation-1",
+                broker_order_id="order-1",
+                from_state="CREATED",
+                to_state="ACCEPTED",
+                safe_reason="BROKER_ACCEPTED",
+                safe_message="accepted",
+                occurred_at=NOW,
+                created_at=NOW,
+            )
+        )
+
+    statements = []
+
+    def capture_statement(_conn, _cursor, sql, _params, _ctx, _many):
+        statements.append(sql)
+
+    event.listen(engine, "before_cursor_execute", capture_statement)
+    with TradingFactsUnitOfWork(factory) as uow:
+        result = uow.orders.execution_diagnostics("scope-1", "automation-1", limit=1)
+        scope = uow.execution_diagnostics_scope("scope-1", "automation-1", limit=20)
+    engine.dispose()
+
+    assert {row["fact_id"] for row in result["accepted_envelopes"]} == {
+        "order-envelope",
+        "execution-envelope",
+        "state-envelope",
+    }
+    assert {row["sequence_number"] for row in result["accepted_envelopes"]} == {4, 6, 9}
+    assert {row["fact_id"] for row in result["orders"]} == {"order-envelope"}
+    order = result["orders"][0]
+    assert order["lot_size"] == 10
+    assert order["decision_id"] == "decision-1"
+    assert order["process_id"] == "process-1"
+    assert order["estimated_commission"] == Decimal("0.01")
+    assert {row["fact_id"] for row in result["executions"]} == {"execution-envelope"}
+    assert result["executions"][0]["broker_commission"] == Decimal("0.01")
+    assert result["executions"][0]["other_fees"] == Decimal("0")
+    assert result["executions"][0]["execution_id"] == "execution-1"
+    assert result["executions"][0]["fact_id"] == "execution-envelope"
+    assert result["executions"][0]["execution_id"] != result["executions"][0]["fact_id"]
+    assert "unlinked-latest-envelope" not in {row["fact_id"] for row in result["accepted_envelopes"]}
+    assert len(result["events"]) == len(result["executions"]) == len(result["orders"]) == 1
+    assert result["truncated"] is True
+    assert scope is not None
+    assert scope["reservations"] == (
+        {
+            "intent_id": "intent-1",
+            "state": "UNKNOWN",
+            "side": "BUY",
+            "origin_runtime_instance_id": "runtime-1",
+            "origin_generation": 2,
+            "quantity_lots": 3,
+            "reserved_cash": Decimal("30"),
+            "reserved_position_units": Decimal("0"),
+            "created_at": NOW,
+            "updated_at": NOW,
+        },
+    )
+    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
+    assert all("FOR UPDATE" not in sql.upper() for sql in statements)
+    assert not any("strategy_snapshot" in sql.lower() or "payload" in sql.lower() for sql in statements)
```

## tests/api/test_execution_diagnostics.py

```diff
--- baseline/tests/api/test_execution_diagnostics.py
+++ tests/api/test_execution_diagnostics.py
@@ -0,0 +1,95 @@
+from decimal import Decimal
+from uuid import uuid4
+
+from fastapi.testclient import TestClient
+from sqlalchemy import create_engine
+from sqlalchemy.orm import sessionmaker
+
+from moex_sentinel.api.app import create_app
+from moex_sentinel.api.auth import hash_password
+from moex_sentinel.storage.models import Base
+from moex_sentinel.storage.models.trading_admission import TradingSubmitReservationModel
+from tests.storage.trading_facts_helpers import NOW, automation_model, instrument_model, user_broker_model
+
+
+def test_scoped_execution_diagnostics_is_authenticated_bounded_and_no_store(tmp_path, monkeypatch):
+    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "TEST")
+    broker_id, automation_id, instrument_id = (str(uuid4()) for _ in range(3))
+    intent_id = str(uuid4())
+    runtime_id = str(uuid4())
+    database_url = f"sqlite:///{tmp_path / 'execution-diagnostics.db'}"
+    engine = create_engine(database_url)
+    Base.metadata.create_all(engine)
+    with sessionmaker(engine).begin() as session:
+        session.add(user_broker_model(broker_id, "account-1"))
+        session.flush()
+        session.add(instrument_model(instrument_id, broker_id))
+        session.flush()
+        session.add(automation_model(automation_id, user_broker_id=broker_id, instrument_id=instrument_id))
+        session.add(
+            TradingSubmitReservationModel(
+                reservation_id=str(uuid4()),
+                user_broker_id=broker_id,
+                automation_id=automation_id,
+                idempotency_key=intent_id,
+                request_fingerprint="a" * 64,
+                state="UNKNOWN",
+                runtime_instance_id=runtime_id,
+                generation=1,
+                configuration_fingerprint="b" * 64,
+                side="BUY",
+                instrument_uid="TEST@TQBR",
+                quantity_lots=2,
+                limit_price=Decimal("10"),
+                requested_amount=Decimal("20"),
+                reserved_cash=Decimal("20"),
+                reserved_position_units=Decimal("0"),
+                payload={"private": "must not be returned"},
+                created_at=NOW,
+                updated_at=NOW,
+            )
+        )
+    engine.dispose()
+
+    with TestClient(create_app(test_auth_bypass=True, database_url=database_url)) as client:
+        response = client.get(f"/api/trading-automations/{automation_id}/execution-diagnostics")
+        invalid_limit = client.get(f"/api/trading-automations/{automation_id}/execution-diagnostics?limit=21")
+        invalid_intent = client.get(f"/api/trading-automations/{automation_id}/execution-diagnostics?intent_id=nope")
+
+    assert response.status_code == 200
+    assert response.headers["Cache-Control"] == "no-store"
+    payload = response.json()
+    assert payload["automation_id"] == automation_id
+    assert payload["broker_id"] == broker_id
+    assert payload["worker_observation"]["observation"] == "UNKNOWN"
+    assert payload["worker_observation"]["reason"] == "NOT_OBSERVED"
+    assert payload["external_instrument_id"] == f"external-{instrument_id}"
+    assert payload["currency"] == "RUB"
+    assert payload["orders"] == payload["events"] == []
+    assert payload["reservations"] == [
+        {
+            "intent_id": intent_id,
+            "state": "UNKNOWN",
+            "side": "BUY",
+            "origin_runtime_instance_id": runtime_id,
+            "origin_generation": 1,
+            "quantity_lots": 2,
+            "reserved_cash": "20.000000000",
+            "reserved_position_units": "0E-9",
+            "created_at": payload["reservations"][0]["created_at"],
+            "updated_at": payload["reservations"][0]["updated_at"],
+        }
+    ]
+    assert payload["executions"] == payload["accepted_envelopes"] == []
+    assert "private" not in response.text
+    assert invalid_limit.status_code == invalid_intent.status_code == 422
+
+
+def test_scoped_execution_diagnostics_requires_operator_auth(tmp_path, monkeypatch):
+    monkeypatch.setenv("AUTH_USERNAME", "operator")
+    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("test password long enough"))
+    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "true")
+    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'auth.db'}")) as client:
+        response = client.get(f"/api/trading-automations/{uuid4()}/execution-diagnostics")
+    assert response.status_code == 401
+    assert response.headers["Cache-Control"] == "no-store"
```

## tests/integration/postgresql/test_execution_diagnostics.py

```diff
--- baseline/tests/integration/postgresql/test_execution_diagnostics.py
+++ tests/integration/postgresql/test_execution_diagnostics.py
@@ -0,0 +1,86 @@
+from decimal import Decimal
+
+from sqlalchemy import create_engine, event, select
+from sqlalchemy.orm import sessionmaker
+
+from moex_sentinel.storage.models.order_facts import BrokerOrderModel
+from moex_sentinel.storage.models.trading_admission import TradingSubmitReservationModel
+from moex_sentinel.storage.models.trading_observability import AutomationEventModel
+from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
+from tests.storage.trading_facts_helpers import NOW, execution_model, seed_order
+
+
+def test_execution_diagnostics_exact_fact_acceptance_on_postgresql(isolated_postgresql_database_url):
+    engine = create_engine(isolated_postgresql_database_url)
+    factory = sessionmaker(engine, expire_on_commit=False)
+    with factory.begin() as session:
+        seed_order(session)
+        session.scalar(select(BrokerOrderModel)).fact_id = "accepted-order-fact"
+        execution = execution_model("execution-1", external_execution_id="fill-1", source="BROKER_FILL")
+        execution.fact_id = "accepted-execution-fact"
+        session.add(execution)
+        session.add(
+            TradingSubmitReservationModel(
+                reservation_id="reservation-1",
+                user_broker_id="scope-1",
+                automation_id="automation-1",
+                idempotency_key="intent-1",
+                request_fingerprint="a" * 64,
+                state="UNKNOWN",
+                runtime_instance_id="runtime-1",
+                generation=2,
+                configuration_fingerprint="b" * 64,
+                side="BUY",
+                instrument_uid="TEST@TQBR",
+                quantity_lots=3,
+                limit_price=Decimal("10"),
+                requested_amount=Decimal("30"),
+                reserved_cash=Decimal("30"),
+                reserved_position_units=Decimal("0"),
+                payload={"private": "not selected"},
+                created_at=NOW,
+                updated_at=NOW,
+            )
+        )
+        for event_id, sequence, kind in (
+            ("accepted-order-fact", 3, "BROKER_ORDER_RECORDED"),
+            ("accepted-execution-fact", 8, "TRADE_EXECUTION_RECORDED"),
+        ):
+            session.add(
+                AutomationEventModel(
+                    event_id=event_id,
+                    automation_id="automation-1",
+                    user_broker_id="scope-1",
+                    sequence_number=sequence,
+                    expected_revision=1,
+                    fact_kind=kind,
+                    safe_message="accepted",
+                    payload={},
+                    occurred_at=NOW,
+                    received_at=NOW,
+                )
+            )
+
+    statements: list[str] = []
+
+    def capture_statement(_conn, _cursor, sql, _params, _ctx, _many):
+        statements.append(sql)
+
+    event.listen(engine, "before_cursor_execute", capture_statement)
+    with TradingFactsUnitOfWork(factory) as uow:
+        projection = uow.orders.execution_diagnostics("scope-1", "automation-1", limit=20)
+        scope = uow.execution_diagnostics_scope("scope-1", "automation-1", limit=20)
+    engine.dispose()
+
+    assert {item["fact_id"] for item in projection["accepted_envelopes"]} == {
+        "accepted-order-fact",
+        "accepted-execution-fact",
+    }
+    assert {item["sequence_number"] for item in projection["accepted_envelopes"]} == {3, 8}
+    assert {item["fact_id"] for item in projection["executions"]} == {"accepted-execution-fact"}
+    assert {item["execution_id"] for item in projection["executions"]} == {"execution-1"}
+    assert projection["executions"][0]["execution_id"] != projection["executions"][0]["fact_id"]
+    assert scope is not None
+    assert scope["reservations"][0]["reserved_cash"] == Decimal("30")
+    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
+    assert all("FOR UPDATE" not in sql.upper() for sql in statements)
```
