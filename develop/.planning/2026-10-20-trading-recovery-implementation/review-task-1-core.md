# task-1-core preliminary review package

Baseline f751ab3+userdirty preserved inbaseline582files. OneD1author; sourcecurrentwhileexpandedCoretests/reportmaystillWIP.

## src/moex_sentinel/domain/execution_diagnostics.py
SHA256 2245882f01d757def3d342cfc04d4288c93fa7ab79cb207aced48be1bf91ae9f
```diff
--- baseline/src/moex_sentinel/domain/execution_diagnostics.py
+++ current/src/moex_sentinel/domain/execution_diagnostics.py
@@ -0,0 +1,28 @@
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
SHA256 4ae9f32a46c41bc4e99bc33a685a2c6226f090c81b015a4a95abd0af25ceb6e4
```diff
--- baseline/src/moex_sentinel/usecases/execution_diagnostics.py
+++ current/src/moex_sentinel/usecases/execution_diagnostics.py
@@ -0,0 +1,79 @@
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
+                automation.broker_id, automation_id, limit=limit,
+                intent_id=None if intent_id is None else str(intent_id),
+            )
+            if scope is None:
+                raise ValueError("Automation diagnostics scope is unavailable")
+            facts = uow.orders.execution_diagnostics(
+                automation.broker_id, automation_id, limit=limit,
+                intent_id=None if intent_id is None else str(intent_id),
+            )
+        worker = worker_store.execution_snapshot(
+            broker_id=UUID(automation.broker_id),
+            automation_id=UUID(automation_id),
+            account_id=str(scope["account_id"]),
+            instrument_id=str(scope["external_instrument_id"]),
+            owner_runtime_instance_id=(
+                None
+                if scope["owner_runtime_instance_id"] is None
+                else UUID(str(scope["owner_runtime_instance_id"]))
+            ),
+            owner_generation=scope["owner_generation"],
+        )
+        worker_payload = {
+            "observation": worker.observation, "reason": worker.reason,
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
+            automation_id=UUID(automation_id), broker_id=UUID(automation.broker_id),
+            account_id=str(scope["account_id"]), instrument_id=UUID(str(scope["instrument_id"])),
+            captured_at=captured_at, worker_observation=worker_payload,
+            owner_runtime_instance_id=(
+                None
+                if scope["owner_runtime_instance_id"] is None
+                else UUID(str(scope["owner_runtime_instance_id"]))
+            ),
+            owner_generation=scope["owner_generation"], orders=facts["orders"],
+            reservations=reservations, events=facts["events"], executions=facts["executions"],
+            accepted_envelopes=facts["accepted_envelopes"],
+            truncated=bool(scope["reservation_truncated"] or facts["truncated"]),
+        )
```

## src/moex_sentinel/views/schemas/execution_diagnostics.py
SHA256 452a8b5087837f10af0d323dea2184e69b55143d06aa7c27446caa7dd9e2e343
```diff
--- baseline/src/moex_sentinel/views/schemas/execution_diagnostics.py
+++ current/src/moex_sentinel/views/schemas/execution_diagnostics.py
@@ -0,0 +1,41 @@
+"""HTTP schema for scoped execution diagnostics."""
+
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
+    captured_at: str
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
+            automation_id=str(value.automation_id), broker_id=str(value.broker_id), account_id=value.account_id,
+            instrument_id=str(value.instrument_id), captured_at=value.captured_at.isoformat(),
+            worker_observation=value.worker_observation,
+            owner_runtime_instance_id=(
+                None if value.owner_runtime_instance_id is None else str(value.owner_runtime_instance_id)
+            ),
+            owner_generation=value.owner_generation, orders=value.orders, reservations=value.reservations,
+            events=value.events, executions=value.executions, accepted_envelopes=value.accepted_envelopes,
+            truncated=value.truncated,
+        )
```

## src/moex_sentinel/views/automations.py
SHA256 fcf614fc2a925c08cd5d9a61b990324d32e447f3211d4834716279876f2e58aa
```diff
--- baseline/src/moex_sentinel/views/automations.py
+++ current/src/moex_sentinel/views/automations.py
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
SHA256 200f1851ba7b295cb9080ff8e8b7025f76c0d8f8e2e520651f3e3425f9ecc370
```diff
--- baseline/src/moex_sentinel/composition.py
+++ current/src/moex_sentinel/composition.py
@@ -60,6 +60,7 @@
     ViewTradingAutomationsUsecase,
     ViewTradingAutomationUsecase,
 )
+from moex_sentinel.usecases.execution_diagnostics import ViewExecutionDiagnosticsUsecase
 from moex_sentinel.usecases.automaton_brokers import (
     ViewAutomatonBrokerConnectionUsecase,
     ViewAutomatonBrokerScopeUsecase,
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
SHA256 56538062d0ae1ba3ba22cc78086e14c6ec053441552ac21e7711a1f2aa085b83
```diff
--- baseline/src/moex_sentinel/storage/repositories/order_facts.py
+++ current/src/moex_sentinel/storage/repositories/order_facts.py
@@ -23,6 +23,7 @@
     TradeExecutionModel,
 )
 from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
+from moex_sentinel.storage.models.trading_observability import AutomationEventModel
 from moex_sentinel.storage.repositories.trading_facts_support import (
     append_idempotent,
     flush_or_translate,
@@ -416,6 +417,113 @@
             raise TradingFactPersistenceError(TradingFactErrorCode.NOT_FOUND, entity_type="broker_order")
         return _order_value(model)
 
+    def execution_diagnostics(
+        self, user_broker_id: str, automation_id: str, *, limit: int, intent_id: str | None = None
+    ):
+        """Read a bounded scalar projection; deliberately does not acquire row locks."""
+        orders = self._session.scalars(
+            select(BrokerOrderModel)
+            .where(
+                BrokerOrderModel.user_broker_id == user_broker_id,
+                BrokerOrderModel.automation_id == automation_id,
+                *(() if intent_id is None else (BrokerOrderModel.idempotency_key == intent_id,)),
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
+            events = self._session.scalars(
+                select(BrokerOrderEventModel)
+                .where(
+                    BrokerOrderEventModel.user_broker_id == user_broker_id,
+                    BrokerOrderEventModel.automation_id == automation_id,
+                    BrokerOrderEventModel.broker_order_id.in_(order_ids),
+                )
+                .order_by(BrokerOrderEventModel.occurred_at.desc(), BrokerOrderEventModel.id)
+                .limit(limit + 1)
+            ).all()
+            facts.extend(row.fact_id for row in events)
+            executions = self._session.scalars(
+                select(TradeExecutionModel)
+                .where(
+                    TradeExecutionModel.user_broker_id == user_broker_id,
+                    TradeExecutionModel.automation_id == automation_id,
+                    TradeExecutionModel.broker_order_id.in_(order_ids),
+                )
+                .order_by(TradeExecutionModel.executed_at.desc(), TradeExecutionModel.id)
+                .limit(limit + 1)
+            ).all()
+            facts.extend(row.fact_id for row in executions)
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
+                .limit(limit + 1)
+            ).all()
+        return {
+            "truncated": truncated or len(events) > limit or len(executions) > limit or len(envelopes) > limit,
+            "orders": tuple({
+                "intent_id": row.idempotency_key, "fact_id": row.fact_id, "order_id": row.id,
+                "external_order_id": row.external_order_id, "state": row.state, "side": row.side,
+                "quantity_lots": row.quantity_lots, "requested_amount": row.requested_amount,
+                "executed_amount": row.executed_amount, "created_at": row.created_at,
+                "dispatch_started_at": row.dispatch_started_at, "broker_responded_at": row.broker_responded_at,
+                "executed_at": row.executed_at, "terminal_at": row.terminal_at,
+            } for row in orders),
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
+                for row in envelopes[:limit]
+            ),
+        }
+
     def list_order_events(self, user_broker_id: str, order_id: str) -> tuple[BrokerOrderEventDraft, ...]:
         statement = (
             select(BrokerOrderEventModel)
```

## src/moex_sentinel/storage/repositories/trading_facts_uow.py
SHA256 3437f049db475dea7983039e9c640be13184557a9ce7d082120729e84194e801
```diff
--- baseline/src/moex_sentinel/storage/repositories/trading_facts_uow.py
+++ current/src/moex_sentinel/storage/repositories/trading_facts_uow.py
@@ -6,6 +6,8 @@
 from sqlalchemy.orm import Session, sessionmaker
 
 from moex_sentinel.domain.trading_facts import FactRuntimeAuthorization, FactSubmitReservation
+from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
+from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
 from moex_sentinel.storage.models.trading_admission import (
     TradingAccountAdmissionModel,
     TradingSubmitReservationModel,
@@ -123,6 +125,66 @@
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
+            )
+            .where(TradingAccountAdmissionModel.user_broker_id == user_broker_id)
+        ).one_or_none()
+        reservation_query = select(TradingSubmitReservationModel).where(
+            TradingSubmitReservationModel.user_broker_id == user_broker_id,
+            TradingSubmitReservationModel.automation_id == automation_id,
+        )
+        if intent_id is not None:
+            reservation_query = reservation_query.where(TradingSubmitReservationModel.idempotency_key == intent_id)
+        reservations = self._session.scalars(
+            reservation_query.order_by(TradingSubmitReservationModel.updated_at.desc()).limit(limit + 1)
+        ).all()
+        return {
+            "automation_id": automation.id,
+            "instrument_id": automation.instrument_id,
+            "account_id": automation.account_id,
+            "external_instrument_id": automation.external_instrument_id,
+            "lot_size": automation.lot_size,
+            "owner_runtime_instance_id": None if owner is None else owner.owner_runtime_instance_id,
+            "owner_generation": None if owner is None else owner.owner_generation,
+            "reservation_truncated": len(reservations) > limit,
+            "reservations": tuple({
+                "intent_id": row.idempotency_key, "state": row.state, "side": row.side,
+                "quantity_lots": row.quantity_lots, "reserved_cash": row.reserved_cash,
+                "reserved_position_units": row.reserved_position_units,
+                "created_at": row.created_at, "updated_at": row.updated_at,
+            } for row in reservations[:limit]),
+        }
 
     def confirm_submit_not_sent(self, user_broker_id: str, idempotency_key: str, *, now) -> bool:
         if self._session is None:
```

## src/moex_sentinel/storage/repositories/trading_audit.py
SHA256 60000d43b5a686d4226445a3995826b2ea0be6401efffc9348598decd03d88cd
```diff
```

## src/moex_sentinel/storage/repositories/trading_admission.py
SHA256 fadd6913f266e2f586c7528c02d6ef1fbe63881a334194a04d603b1265fb103c
```diff
```

## src/moex_sentinel/services/trading_fact_ports.py
SHA256 c613534c42f7693fcfa9925d8d804b3ec57d5c0d6ec93bc716225c8b095d688d
```diff
--- baseline/src/moex_sentinel/services/trading_fact_ports.py
+++ current/src/moex_sentinel/services/trading_fact_ports.py
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
SHA256 2e05f1e4ed47fe2deeee0a014d3d4fd01d2a8c071715fab8b78a373a05030269
```diff
--- baseline/tests/storage/test_execution_diagnostics.py
+++ current/tests/storage/test_execution_diagnostics.py
@@ -0,0 +1,53 @@
+from sqlalchemy import create_engine, event, select
+from sqlalchemy.orm import sessionmaker
+
+from moex_sentinel.storage.models import Base
+from moex_sentinel.storage.models.order_facts import BrokerOrderModel
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
+        session.add_all(
+            [
+                AutomationEventModel(
+                    event_id="order-envelope", automation_id="automation-1", user_broker_id="scope-1",
+                    sequence_number=4, expected_revision=1, fact_kind="BROKER_ORDER_RECORDED",
+                    safe_message="order accepted", payload={}, occurred_at=NOW, received_at=NOW,
+                ),
+                AutomationEventModel(
+                    event_id="execution-envelope", automation_id="automation-1", user_broker_id="scope-1",
+                    sequence_number=9, expected_revision=1, fact_kind="TRADE_EXECUTION_RECORDED",
+                    safe_message="execution accepted", payload={}, occurred_at=NOW, received_at=NOW,
+                ),
+            ]
+        )
+
+    statements = []
+
+    def capture_statement(_conn, _cursor, sql, _params, _ctx, _many):
+        statements.append(sql)
+
+    event.listen(engine, "before_cursor_execute", capture_statement)
+    with TradingFactsUnitOfWork(factory) as uow:
+        result = uow.orders.execution_diagnostics("scope-1", "automation-1", limit=20)
+    engine.dispose()
+
+    assert {row["fact_id"] for row in result["accepted_envelopes"]} == {
+        "order-envelope", "execution-envelope"
+    }
+    assert {row["sequence_number"] for row in result["accepted_envelopes"]} == {4, 9}
+    assert {row["fact_id"] for row in result["orders"]} == {"order-envelope"}
+    assert {row["fact_id"] for row in result["executions"]} == {"execution-envelope"}
+    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
+    assert all("FOR UPDATE" not in sql.upper() for sql in statements)
```

## tests/api/test_execution_diagnostics.py
SHA256 0bca1698723747b696cc8ecde29c62cefd82da2ea1d773a28c0786963fadb250
```diff
--- baseline/tests/api/test_execution_diagnostics.py
+++ current/tests/api/test_execution_diagnostics.py
@@ -0,0 +1,51 @@
+from uuid import uuid4
+
+from fastapi.testclient import TestClient
+from sqlalchemy import create_engine
+from sqlalchemy.orm import sessionmaker
+
+from moex_sentinel.api.app import create_app
+from moex_sentinel.api.auth import hash_password
+from moex_sentinel.storage.models import Base
+from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model
+
+
+def test_scoped_execution_diagnostics_is_authenticated_bounded_and_no_store(tmp_path, monkeypatch):
+    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "TEST")
+    broker_id, automation_id, instrument_id = (str(uuid4()) for _ in range(3))
+    database_url = f"sqlite:///{tmp_path / 'execution-diagnostics.db'}"
+    engine = create_engine(database_url)
+    Base.metadata.create_all(engine)
+    with sessionmaker(engine).begin() as session:
+        session.add(user_broker_model(broker_id, "account-1"))
+        session.flush()
+        session.add(instrument_model(instrument_id, broker_id))
+        session.flush()
+        session.add(automation_model(automation_id, user_broker_id=broker_id, instrument_id=instrument_id))
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
+    assert payload["orders"] == payload["reservations"] == payload["events"] == []
+    assert payload["executions"] == payload["accepted_envelopes"] == []
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

Pending test/file: tests/integration/postgresql/test_execution_diagnostics.py
