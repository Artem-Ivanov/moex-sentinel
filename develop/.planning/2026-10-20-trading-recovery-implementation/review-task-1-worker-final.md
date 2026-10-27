# D1 worker: final delta from pre-implementation dirty baseline
Source/test hashes: sibling final-hashes.json. Prior dirty work must be preserved. Author report: task-1-report.md. Full backend suite with isolated PostgreSQL is running; no final test claim yet.

## src/sentinel_contracts/worker_diagnostics.py

```diff
--- baseline/src/sentinel_contracts/worker_diagnostics.py
+++ src/sentinel_contracts/worker_diagnostics.py
@@ -1,17 +1,19 @@
 """Safe volatile control progress and Worker-owned outbox observations."""
 
 from datetime import datetime
+from decimal import Decimal
 from typing import Annotated, Literal, Self
 from uuid import UUID
 
 from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
 
-from sentinel_contracts.broker_execution import BrokerOrderStatus
+from sentinel_contracts.broker_execution import BrokerOrderStatus, OrderSide
 from sentinel_contracts.time import floor_utc_millisecond
 from sentinel_contracts.trading import AutomationState
 
 Count = Annotated[int, Field(ge=0, strict=True)]
 IterationResult = Literal["COMPLETED", "OUTBOX_BLOCKED", "ERROR"]
+DiagnosticAmount = Annotated[Decimal, Field(ge=0, max_digits=28, decimal_places=9)]
 
 
 class BrokerRuntimeDiagnostics(BaseModel):
@@ -221,6 +223,117 @@
         return self
 
 
+class WorkerIntentDiagnostics(BaseModel):
+    """One redacted local intent row, with no strategy or transport payload."""
+
+    model_config = ConfigDict(extra="forbid", frozen=True)
+
+    intent_id: UUID
+    decision_id: UUID | None
+    process_id: UUID | None
+    active: bool
+    state: BrokerOrderStatus
+    side: OrderSide
+    external_order_id: str | None = Field(default=None, max_length=128)
+    quantity_lots: Annotated[int, Field(gt=0, strict=True)]
+    limit_price: DiagnosticAmount
+    requested_amount: DiagnosticAmount
+    executed_lots: Count
+    executed_amount: DiagnosticAmount
+    executed_price: DiagnosticAmount
+    estimated_commission: DiagnosticAmount
+    executed_commission: DiagnosticAmount
+    currency: str | None = Field(default=None, max_length=8)
+    fact_execution_id: UUID | None
+    execution_facts_emitted_at: datetime | None
+    origin_runtime_instance_id: UUID | None
+    origin_generation: Annotated[int, Field(gt=0, strict=True)] | None
+    created_at: datetime
+    updated_at: datetime
+    dispatch_started_at: datetime | None
+    broker_responded_at: datetime | None
+    executed_at: datetime | None
+    terminal_at: datetime | None
+
+    @field_validator(
+        "created_at",
+        "updated_at",
+        "dispatch_started_at",
+        "broker_responded_at",
+        "executed_at",
+        "terminal_at",
+        "execution_facts_emitted_at",
+    )
+    @classmethod
+    def normalize_time(cls, value: datetime | None) -> datetime | None:
+        return None if value is None else floor_utc_millisecond(value)
+
+    @model_validator(mode="after")
+    def consistent_origin(self) -> Self:
+        if (self.origin_runtime_instance_id is None) != (self.origin_generation is None):
+            raise ValueError("Intent origin requires both runtime and generation")
+        return self
+
+
+class WorkerAutomationExecutionDiagnostics(BaseModel):
+    """Bounded intent history and immutable automation bindings."""
+
+    model_config = ConfigDict(extra="forbid", frozen=True)
+
+    automation_id: UUID
+    account_id: str = Field(min_length=1, max_length=128)
+    external_instrument_id: str = Field(min_length=1, max_length=128)
+    fact_instrument_id: UUID | None
+    lot_size: Annotated[int, Field(gt=0, strict=True)]
+    pending_fact_count: Count
+    failed_fact_count: Count
+    intents: tuple[WorkerIntentDiagnostics, ...] = Field(default=(), max_length=5)
+    history_truncated: bool = False
+
+    @model_validator(mode="after")
+    def active_first(self) -> Self:
+        active = tuple(intent.active for intent in self.intents)
+        if active != tuple(sorted(active, reverse=True)):
+            raise ValueError("Active intents must precede terminal history")
+        return self
+
+
+class WorkerExecutionDiagnostics(BaseModel):
+    """Optional, bounded Worker-local execution projection."""
+
+    model_config = ConfigDict(extra="forbid", frozen=True)
+
+    observation: Literal["OBSERVED", "UNKNOWN"] = "UNKNOWN"
+    reason: Literal[
+        "OBSERVED",
+        "READ_FAILED",
+        "INVALID_PROJECTION",
+        "UNCONFIGURED_BROKER",
+        "UNSCOPED_CACHE",
+        "BUDGET_EXCEEDED",
+        "UNSUPPORTED_SOURCE",
+        "UNAUTHENTICATED_SOURCE",
+    ] = "READ_FAILED"
+    broker_id: UUID | None = None
+    captured_at: datetime | None = None
+    rows: tuple[WorkerAutomationExecutionDiagnostics, ...] = Field(default=(), max_length=100)
+    truncated: bool = False
+
+    @field_validator("captured_at")
+    @classmethod
+    def normalize_captured_at(cls, value: datetime | None) -> datetime | None:
+        return None if value is None else floor_utc_millisecond(value)
+
+    @model_validator(mode="after")
+    def consistent_observation(self) -> Self:
+        if self.observation == "UNKNOWN":
+            if self.reason == "OBSERVED" or self.captured_at is not None or self.rows or self.truncated:
+                raise ValueError("Unknown execution projection cannot contain financial rows")
+        elif self.reason != "OBSERVED" or self.broker_id is None or self.captured_at is None:
+            raise ValueError("Observed execution projection requires scope and capture time")
+        return self
+
+
 class WorkerDiagnostics(BaseModel):
     model_config = ConfigDict(extra="forbid", frozen=True)
 
@@ -232,6 +345,7 @@
     outbox: OutboxDiagnostics
     broker_runtime: BrokerRuntimeDiagnostics = Field(default_factory=BrokerRuntimeDiagnostics)
     command_sync: CommandSyncDiagnostics | None = None
+    execution_diagnostics: WorkerExecutionDiagnostics | None = None
 
     @field_validator("last_completed_at", "last_finished_at")
     @classmethod
```

## src/trading_automaton/storage/execution_diagnostics.py

```diff
--- baseline/src/trading_automaton/storage/execution_diagnostics.py
+++ src/trading_automaton/storage/execution_diagnostics.py
@@ -0,0 +1,326 @@
+"""Bounded read-only SQLite projection for optional Worker diagnostics."""
+
+import sqlite3
+from contextlib import suppress
+from datetime import UTC, datetime
+from pathlib import Path
+from time import monotonic
+from urllib.parse import quote
+from uuid import UUID
+
+from sqlalchemy import bindparam, create_engine, text
+from sqlalchemy.engine import URL, make_url
+from sqlalchemy.exc import OperationalError
+from sqlalchemy.pool import NullPool
+
+from sentinel_contracts.broker_order_policy import LEGACY_TERMINAL_STATES
+from sentinel_contracts.time import floor_utc_millisecond
+from sentinel_contracts.worker_diagnostics import (
+    WorkerAutomationExecutionDiagnostics,
+    WorkerExecutionDiagnostics,
+    WorkerIntentDiagnostics,
+)
+
+_READ_BUDGET_SECONDS = 0.1
+_CONNECT_TIMEOUT_SECONDS = 0.05
+_MAX_AUTOMATIONS = 100
+_MAX_INTENTS_PER_AUTOMATION = 5
+_TERMINAL = tuple(sorted(status.value for status in LEGACY_TERMINAL_STATES))
+_ACTIVE_STATES = (
+    "CREATED",
+    "DISPATCH_PENDING",
+    "SUBMITTING",
+    "SUBMITTED",
+    "ACCEPTED",
+    "PARTIALLY_FILLED",
+    "UNCERTAIN",
+)
+
+_AUTOMATIONS = text(
+    """
+    SELECT automation_id, account_id, instrument_id, fact_instrument_id, lot_size
+    FROM cached_automations
+    WHERE broker_id = :broker_id
+    ORDER BY automation_id
+    LIMIT 101
+    """
+)
+_UNSCOPED_CACHE = text("SELECT 1 FROM cached_automations WHERE broker_id IS NULL LIMIT 1")
+_INTENTS = text(
+    """
+    SELECT automation_id, idempotency_key, decision_id, process_id, state, side,
+           broker_order_id, quantity_lots, limit_price, requested_amount,
+           executed_lots, executed_amount, estimated_commission, executed_commission,
+           executed_price,
+           execution_currency, fact_execution_id, execution_facts_emitted_at,
+           origin_runtime_instance_id, origin_generation, created_at, updated_at,
+           dispatch_started_at, broker_responded_at, executed_at, terminal_at,
+           is_active
+    FROM (
+        SELECT intent.automation_id, intent.idempotency_key,
+               decision.id AS decision_id, decision.process_id,
+               intent.state, intent.side, intent.broker_order_id,
+               intent.quantity_lots, intent.limit_price, intent.requested_amount,
+               intent.executed_lots, intent.executed_amount,
+               intent.executed_price,
+               intent.estimated_commission, intent.executed_commission,
+               intent.execution_currency, intent.fact_execution_id,
+               intent.execution_facts_emitted_at,
+               json_extract(intent.strategy_snapshot, '$.admission_origin.runtime_instance_id')
+                   AS origin_runtime_instance_id,
+               json_extract(intent.strategy_snapshot, '$.admission_origin.generation')
+                   AS origin_generation,
+               intent.created_at, intent.updated_at, intent.dispatch_started_at,
+               intent.broker_responded_at, intent.executed_at, intent.terminal_at,
+               CASE WHEN (
+                   intent.state IN :active_states
+                   OR (intent.state = 'FAILED' AND intent.terminal_at IS NULL)
+                   OR (:prod_terminal_unfinalized AND intent.state IN :terminal_states AND intent.terminal_at IS NULL)
+                   OR (:test_filled_unemitted AND intent.state = 'FILLED'
+                       AND intent.execution_facts_emitted_at IS NULL)
+               ) THEN 1 ELSE 0 END AS is_active,
+               row_number() OVER (
+                   PARTITION BY intent.automation_id
+                   ORDER BY CASE WHEN (
+                       intent.state IN :active_states
+                       OR (intent.state = 'FAILED' AND intent.terminal_at IS NULL)
+                       OR (:prod_terminal_unfinalized AND intent.state IN :terminal_states
+                           AND intent.terminal_at IS NULL)
+                       OR (:test_filled_unemitted AND intent.state = 'FILLED'
+                           AND intent.execution_facts_emitted_at IS NULL)
+                   ) THEN 0 ELSE 1 END,
+                            intent.updated_at DESC, intent.idempotency_key DESC
+               ) AS position
+        FROM broker_intents AS intent
+        LEFT JOIN trade_decisions AS decision
+          ON decision.id = (
+              SELECT candidate.id FROM trade_decisions AS candidate
+              WHERE candidate.intent_id = intent.idempotency_key
+              ORDER BY candidate.occurred_at DESC, candidate.id DESC LIMIT 1
+          )
+        WHERE intent.automation_id IN :automation_ids
+    )
+    WHERE position <= 6
+    ORDER BY automation_id, position
+    """
+).bindparams(
+    bindparam("automation_ids", expanding=True),
+    bindparam("active_states", expanding=True),
+    bindparam("terminal_states", expanding=True),
+)
+_FACT_COUNTS = text(
+    """
+    SELECT automation_id,
+           sum(CASE WHEN delivery_state = 'PENDING' THEN 1 ELSE 0 END) AS pending_count,
+           sum(CASE WHEN delivery_state = 'FAILED' THEN 1 ELSE 0 END) AS failed_count
+    FROM fact_outbox
+    WHERE automation_id IN :automation_ids
+    GROUP BY automation_id
+    """
+).bindparams(bindparam("automation_ids", expanding=True))
+
+
+class WorkerExecutionDiagnosticsReader:
+    """Read existing Worker state using an independent, bounded SQLite connection."""
+
+    def __init__(self, database_url: str, *, environment: str = "TEST") -> None:
+        if environment not in {"TEST", "PROD"}:
+            raise ValueError("Invalid Worker diagnostics environment")
+        self._database_url = database_url
+        self._environment = environment
+
+    def read(self, broker_id: UUID | str | None, captured_at: datetime) -> WorkerExecutionDiagnostics:
+        if broker_id is None:
+            return WorkerExecutionDiagnostics(reason="UNCONFIGURED_BROKER")
+        try:
+            broker_uuid = UUID(str(broker_id))
+        except (TypeError, ValueError):
+            return WorkerExecutionDiagnostics(reason="UNCONFIGURED_BROKER")
+
+        started = monotonic()
+        deadline = started + _READ_BUDGET_SECONDS
+        engine = None
+        connection = None
+        try:
+            database_path = self._database_path()
+            if database_path is None:
+                return WorkerExecutionDiagnostics(reason="UNSUPPORTED_SOURCE", broker_id=broker_uuid)
+            if not database_path.is_file():
+                return WorkerExecutionDiagnostics(reason="UNSUPPORTED_SOURCE", broker_id=broker_uuid)
+            self._check_budget(deadline)
+            uri = f"file:{quote(str(database_path), safe='/')}"
+            engine = create_engine(
+                URL.create("sqlite+pysqlite", database=uri, query={"mode": "ro", "uri": "true"}),
+                poolclass=NullPool,
+                connect_args={"timeout": min(_CONNECT_TIMEOUT_SECONDS, self._remaining(deadline))},
+            )
+            connection = engine.connect()
+            raw = connection.connection.driver_connection
+            raw.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
+            self._check_budget(deadline)
+            connection.exec_driver_sql("PRAGMA query_only=ON")
+            connection.exec_driver_sql("BEGIN")
+            self._set_busy_timeout(connection, deadline)
+            if connection.execute(_UNSCOPED_CACHE).first() is not None:
+                return WorkerExecutionDiagnostics(
+                    reason="UNSCOPED_CACHE",
+                    broker_id=broker_uuid,
+                )
+            automation_rows = connection.execute(_AUTOMATIONS, {"broker_id": str(broker_uuid)}).mappings().all()
+            self._check_budget(deadline)
+            truncated = len(automation_rows) > _MAX_AUTOMATIONS
+            automation_rows = automation_rows[:_MAX_AUTOMATIONS]
+            if not automation_rows:
+                return WorkerExecutionDiagnostics(
+                    observation="OBSERVED",
+                    reason="OBSERVED",
+                    broker_id=broker_uuid,
+                    captured_at=floor_utc_millisecond(captured_at),
+                )
+            if any(
+                not row["account_id"]
+                or not row["instrument_id"]
+                or not row["lot_size"]
+                or row["fact_instrument_id"] is None
+                for row in automation_rows
+            ):
+                return WorkerExecutionDiagnostics(reason="UNSCOPED_CACHE", broker_id=broker_uuid)
+
+            automation_ids = [row["automation_id"] for row in automation_rows]
+            parameters = {
+                "automation_ids": tuple(automation_ids),
+                "active_states": _ACTIVE_STATES,
+                "terminal_states": _TERMINAL,
+                "prod_terminal_unfinalized": self._environment == "PROD",
+                "test_filled_unemitted": self._environment == "TEST",
+            }
+            self._set_busy_timeout(connection, deadline)
+            intent_rows = connection.execute(_INTENTS, parameters).mappings().all()
+            self._check_budget(deadline)
+            self._set_busy_timeout(connection, deadline)
+            fact_rows = connection.execute(_FACT_COUNTS, parameters).mappings().all()
+            self._check_budget(deadline)
+
+            intents_by_automation: dict[str, list[WorkerIntentDiagnostics]] = {}
+            overflowed: set[str] = set()
+            for row in intent_rows:
+                intents = intents_by_automation.setdefault(row["automation_id"], [])
+                if len(intents) == _MAX_INTENTS_PER_AUTOMATION:
+                    overflowed.add(row["automation_id"])
+                    continue
+                intents.append(
+                    WorkerIntentDiagnostics(
+                        intent_id=row["idempotency_key"],
+                        decision_id=row["decision_id"],
+                        process_id=row["process_id"],
+                        active=bool(row["is_active"]),
+                        state=row["state"],
+                        side=row["side"],
+                        external_order_id=row["broker_order_id"],
+                        quantity_lots=row["quantity_lots"],
+                        limit_price=row["limit_price"],
+                        requested_amount=row["requested_amount"],
+                        executed_lots=row["executed_lots"],
+                        executed_amount=row["executed_amount"],
+                        executed_price=row["executed_price"],
+                        estimated_commission=row["estimated_commission"],
+                        executed_commission=row["executed_commission"],
+                        currency=row["execution_currency"],
+                        fact_execution_id=row["fact_execution_id"],
+                        origin_runtime_instance_id=row["origin_runtime_instance_id"],
+                        origin_generation=row["origin_generation"],
+                        created_at=_as_utc(row["created_at"]),
+                        updated_at=_as_utc(row["updated_at"]),
+                        dispatch_started_at=_as_utc(row["dispatch_started_at"]),
+                        broker_responded_at=_as_utc(row["broker_responded_at"]),
+                        executed_at=_as_utc(row["executed_at"]),
+                        terminal_at=_as_utc(row["terminal_at"]),
+                        execution_facts_emitted_at=_as_utc(row["execution_facts_emitted_at"]),
+                    )
+                )
+            counts = {row["automation_id"]: row for row in fact_rows}
+            self._check_budget(deadline)
+            rows = tuple(
+                WorkerAutomationExecutionDiagnostics(
+                    automation_id=record["automation_id"],
+                    account_id=record["account_id"],
+                    external_instrument_id=record["instrument_id"],
+                    fact_instrument_id=record["fact_instrument_id"],
+                    lot_size=record["lot_size"],
+                    pending_fact_count=(counts.get(record["automation_id"]) or {}).get("pending_count") or 0,
+                    failed_fact_count=(counts.get(record["automation_id"]) or {}).get("failed_count") or 0,
+                    intents=tuple(intents_by_automation.get(record["automation_id"], ())),
+                    history_truncated=record["automation_id"] in overflowed,
+                )
+                for record in automation_rows
+            )
+            self._check_budget(deadline)
+            return WorkerExecutionDiagnostics(
+                observation="OBSERVED",
+                reason="OBSERVED",
+                broker_id=broker_uuid,
+                captured_at=floor_utc_millisecond(captured_at),
+                rows=rows,
+                truncated=truncated,
+            )
+        except _BudgetExceeded:
+            return WorkerExecutionDiagnostics(reason="BUDGET_EXCEEDED", broker_id=broker_uuid)
+        except (sqlite3.OperationalError, OperationalError) as error:
+            if monotonic() >= deadline or "locked" in str(error).lower() or "busy" in str(error).lower():
+                return WorkerExecutionDiagnostics(reason="BUDGET_EXCEEDED", broker_id=broker_uuid)
+            return WorkerExecutionDiagnostics(reason="READ_FAILED", broker_id=broker_uuid)
+        except Exception:
+            return WorkerExecutionDiagnostics(reason="READ_FAILED", broker_id=broker_uuid)
+        finally:
+            if connection is not None:
+                with suppress(Exception):
+                    connection.connection.driver_connection.set_progress_handler(None, 0)
+                with suppress(Exception):
+                    connection.rollback()
+                with suppress(Exception):
+                    connection.close()
+            if engine is not None:
+                engine.dispose()
+
+    def _database_path(self) -> Path | None:
+        try:
+            url = make_url(self._database_url)
+        except Exception:
+            return None
+        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
+            return None
+        if url.query:
+            return None
+        try:
+            return Path(url.database).resolve(strict=True)
+        except OSError:
+            return None
+
+    @staticmethod
+    def _remaining(deadline: float) -> float:
+        remaining = deadline - monotonic()
+        if remaining <= 0:
+            raise _BudgetExceeded
+        return remaining
+
+    @classmethod
+    def _check_budget(cls, deadline: float) -> None:
+        cls._remaining(deadline)
+
+    @classmethod
+    def _set_busy_timeout(cls, connection, deadline: float) -> None:
+        milliseconds = max(1, min(50, int(cls._remaining(deadline) * 1000)))
+        connection.exec_driver_sql(f"PRAGMA busy_timeout={milliseconds}")
+
+
+class _BudgetExceeded(Exception):
+    pass
+
+
+def _as_utc(value: datetime | str | None) -> datetime | None:
+    if value is None:
+        return None
+    timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
+    if timestamp.tzinfo is None:
+        return timestamp.replace(tzinfo=UTC)
+    return timestamp.astimezone(UTC)
```

## src/trading_automaton/runtime/streaming_coordinator.py

```diff
--- baseline/src/trading_automaton/runtime/streaming_coordinator.py
+++ src/trading_automaton/runtime/streaming_coordinator.py
@@ -16,6 +16,7 @@
     IterationResult,
     OutboxDiagnostics,
     WorkerDiagnostics,
+    WorkerExecutionDiagnostics,
 )
 from trading_automaton.usecases.synchronize_runtime import (
     SynchronizeTradingRuntimeUsecase,
@@ -143,12 +144,14 @@
         monotonic: Callable[[], float] = system_monotonic,
         heartbeat_interval_seconds: float = 3.0,
         outbox_diagnostics: Callable[[], OutboxDiagnostics] | None = None,
+        execution_diagnostics: Callable[[datetime], WorkerExecutionDiagnostics] | None = None,
         command_sync_diagnostics: (
             Callable[[set[str] | None, int, Literal["REPLACE_FAILED", "SYNC_FAILED"] | None], CommandSyncDiagnostics]
             | None
         ) = None,
     ) -> None:
         self._outbox_diagnostics = outbox_diagnostics
+        self._execution_diagnostics = execution_diagnostics
         self._command_sync_diagnostics = command_sync_diagnostics
         self._command_sync_snapshot: CommandSyncDiagnostics | None = None
         self._completed_iterations = 0
@@ -323,6 +326,13 @@
                 outbox = self._outbox_diagnostics()
             except Exception as error:
                 LOGGER.warning("Outbox observation failed: %s", type(error).__name__)
+        execution = None
+        if self._execution_diagnostics is not None:
+            try:
+                execution = self._execution_diagnostics(self._now())
+            except Exception as error:
+                LOGGER.warning("Execution observation failed: %s", type(error).__name__)
+                execution = WorkerExecutionDiagnostics(reason="READ_FAILED")
         try:
             self._core.heartbeat(
                 self._worker_id,
@@ -336,6 +346,7 @@
                     outbox=outbox,
                     broker_runtime=broker_runtime,
                     command_sync=command_sync,
+                    execution_diagnostics=execution,
                 ),
             )
         except Exception as error:
```

## src/trading_automaton/composition.py

```diff
--- baseline/src/trading_automaton/composition.py
+++ src/trading_automaton/composition.py
@@ -89,6 +89,7 @@
     create_worker_engine,
     initialize_worker_schema,
 )
+from trading_automaton.storage.execution_diagnostics import WorkerExecutionDiagnosticsReader
 from trading_automaton.storage.repository import LocalAutomationRepository
 from trading_automaton.usecases.broker_iteration import RunBrokerIterationUsecase
 from trading_automaton.usecases.recover_worker_run import RecoverWorkerRunUsecase
@@ -402,6 +403,9 @@
         raise ValueError("READ_ONLY requires strategy disabled")
     engine = create_worker_engine(settings.database_url)
     initialize_worker_schema(engine)
+    execution_diagnostics_reader = WorkerExecutionDiagnosticsReader(
+        settings.database_url, environment=settings.application_environment
+    )
     repository = LocalAutomationRepository(
         sessionmaker(engine, expire_on_commit=False),
         environment=settings.application_environment,
@@ -506,6 +510,10 @@
         now=now,
         heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
         outbox_diagnostics=repository.outbox_diagnostics,
+        execution_diagnostics=lambda captured_at: execution_diagnostics_reader.read(
+            settings.internal_worker_broker_id,
+            captured_at,
+        ),
         command_sync_diagnostics=lambda grouped_ids, claimed_count, publication_reason: (
             repository.command_sync_diagnostics(
                 (None if settings.internal_worker_broker_id is None else str(settings.internal_worker_broker_id)),
```

## src/trading_automaton/adapters/core_client.py

```diff
--- baseline/src/trading_automaton/adapters/core_client.py
+++ src/trading_automaton/adapters/core_client.py
@@ -57,6 +57,7 @@
         self._strategy_enabled = strategy_enabled
         self._registration: RuntimeRegistrationResult | None = None
         self._owner_expired = False
+        self._execution_diagnostics_supported = False
 
     def validate_runtime(self) -> None:
         if self._application_environment is None and self._access_mode is None:
@@ -71,6 +72,7 @@
             raise ValueError("Worker/Core environment mismatch")
         if configuration.get("access_mode") != self._access_mode:
             raise ValueError("Worker/Core access mode mismatch")
+        self._execution_diagnostics_supported = configuration.get("execution_diagnostics") == "v1"
 
     def claim_commands(self, worker_id: str, limit: int) -> list[AutomationCommand]:
         self.validate_runtime()
@@ -109,13 +111,18 @@
     def heartbeat(self, worker_id: str, occurred_at: datetime, diagnostics: WorkerDiagnostics | None = None) -> None:
         self.validate_runtime()
         self.renew_admission()
+        worker_diagnostics = None
+        if diagnostics is not None:
+            worker_diagnostics = diagnostics.model_dump(mode="json")
+            if not self._execution_diagnostics_supported:
+                worker_diagnostics.pop("execution_diagnostics", None)
         self._request(
             "POST",
             "/internal/automaton/heartbeats",
             json={
                 "worker_id": worker_id,
                 "occurred_at": _json_value(occurred_at),
-                **({"worker_diagnostics": diagnostics.model_dump(mode="json")} if diagnostics is not None else {}),
+                **({"worker_diagnostics": worker_diagnostics} if worker_diagnostics is not None else {}),
                 **(
                     {
                         "runtime_version": {
```

## src/moex_sentinel/api/app.py

```diff
--- baseline/src/moex_sentinel/api/app.py
+++ src/moex_sentinel/api/app.py
@@ -467,6 +467,13 @@
         return response
 
     @application.get("/internal/runtime")
+    async def internal_runtime_configuration() -> dict[str, str]:
+        return {
+            "environment": settings.application_environment,
+            "access_mode": settings.broker_access_mode,
+            "execution_diagnostics": "v1",
+        }
+
     @application.get("/api/runtime")
     async def runtime_configuration() -> dict[str, str]:
         return {"environment": settings.application_environment, "access_mode": settings.broker_access_mode}
```

## src/moex_sentinel/views/schemas/automations.py

```diff
--- baseline/src/moex_sentinel/views/schemas/automations.py
+++ src/moex_sentinel/views/schemas/automations.py
@@ -6,7 +6,7 @@
 from decimal import Decimal
 from typing import TYPE_CHECKING, Any
 
-from pydantic import ConfigDict, Field, StrictInt, model_validator
+from pydantic import ConfigDict, Field, StrictInt, ValidationError, model_validator
 
 from moex_sentinel.domain.repository_records import AutomationRecord
 from moex_sentinel.views.schemas.market_data import HistoricCandleSchema
@@ -23,7 +23,7 @@
 from sentinel_contracts.tinvest import BrokerAccessMode, BrokerEnvironment
 from sentinel_contracts.trading import AutomationState
 from sentinel_contracts.trading_facts import OrderQuotaPercent
-from sentinel_contracts.worker_diagnostics import WorkerDiagnostics
+from sentinel_contracts.worker_diagnostics import WorkerDiagnostics, WorkerExecutionDiagnostics
 
 if TYPE_CHECKING:
     from moex_sentinel.domain.automations import TradingAutomationDetails
@@ -184,6 +184,50 @@
     runtime_version: RuntimeVersion | None = None
     worker_diagnostics: WorkerDiagnostics | None = None
 
+    @model_validator(mode="before")
+    @classmethod
+    def contain_invalid_execution_projection(cls, value):
+        if not isinstance(value, dict):
+            return value
+        diagnostics = value.get("worker_diagnostics")
+        if not isinstance(diagnostics, dict) or "execution_diagnostics" not in diagnostics:
+            return value
+        raw_execution = diagnostics["execution_diagnostics"]
+        if raw_execution is None:
+            return value
+        oversized = False
+        if isinstance(raw_execution, dict):
+            rows = raw_execution.get("rows")
+            if isinstance(rows, (list, tuple)):
+                oversized = len(rows) > 100
+                if not oversized:
+                    oversized = any(
+                        isinstance(row, dict)
+                        and isinstance(row.get("intents"), (list, tuple))
+                        and len(row["intents"]) > 5
+                        for row in rows
+                    )
+        if oversized:
+            value = dict(value)
+            diagnostics = dict(diagnostics)
+            diagnostics["execution_diagnostics"] = {
+                "observation": "UNKNOWN",
+                "reason": "INVALID_PROJECTION",
+            }
+            value["worker_diagnostics"] = diagnostics
+            return value
+        try:
+            WorkerExecutionDiagnostics.model_validate(raw_execution)
+        except ValidationError:
+            value = dict(value)
+            diagnostics = dict(diagnostics)
+            diagnostics["execution_diagnostics"] = {
+                "observation": "UNKNOWN",
+                "reason": "INVALID_PROJECTION",
+            }
+            value["worker_diagnostics"] = diagnostics
+        return value
+
     @model_validator(mode="after")
     def require_diagnostic_identity(self):
         if self.worker_diagnostics is not None and self.runtime_version is None:
```

## src/moex_sentinel/views/internal_automaton.py

```diff
--- baseline/src/moex_sentinel/views/internal_automaton.py
+++ src/moex_sentinel/views/internal_automaton.py
@@ -13,6 +13,7 @@
     HeartbeatResponseSchema,
 )
 from sentinel_contracts.broker_execution import BrokerScope
+from sentinel_contracts.worker_diagnostics import WorkerExecutionDiagnostics
 
 router = APIRouter(prefix="/internal/automaton", tags=["internal-automaton"])
 
@@ -22,10 +23,34 @@
     principal = getattr(request.state, "worker_principal", None)
     if principal is not None and payload.worker_id != principal.worker_id:
         return JSONResponse({"detail": "Worker identity is outside the configured scope"}, status_code=403)  # type: ignore[return-value]
+    diagnostics = payload.worker_diagnostics
+    if (
+        principal is not None
+        and payload.runtime_version is not None
+        and (
+            payload.runtime_version.instance_id != principal.runtime_instance_id
+            or payload.runtime_version.environment != principal.environment
+            or payload.runtime_version.access_mode != principal.access_mode
+        )
+    ):
+        return JSONResponse({"detail": "Worker runtime identity is outside the configured scope"}, status_code=403)  # type: ignore[return-value]
+    if diagnostics is not None and diagnostics.execution_diagnostics is not None:
+        execution = diagnostics.execution_diagnostics
+        if principal is not None and execution.broker_id is not None and execution.broker_id != principal.broker_id:
+            return JSONResponse({"detail": "Worker broker identity is outside the configured scope"}, status_code=403)  # type: ignore[return-value]
+        if principal is None and execution.observation == "OBSERVED":
+            diagnostics = diagnostics.model_copy(
+                update={"execution_diagnostics": WorkerExecutionDiagnostics(reason="UNAUTHENTICATED_SOURCE")}
+            )
     result = _usecases(request).record_automaton_heartbeat.execute(payload.worker_id, payload.occurred_at)
     if payload.runtime_version is not None:
         request.app.state.worker_versions.observe(
-            payload.runtime_version, payload.occurred_at, payload.worker_diagnostics
+            payload.runtime_version,
+            payload.occurred_at,
+            diagnostics,
+            source_runtime_instance_id=None if principal is None else principal.runtime_instance_id,
+            source_generation=None if principal is None else principal.generation,
+            source_broker_id=None if principal is None else principal.broker_id,
         )
     return HeartbeatResponseSchema.model_validate(result, from_attributes=True)
 
```

## src/moex_sentinel/services/runtime_versions.py

```diff
--- baseline/src/moex_sentinel/services/runtime_versions.py
+++ src/moex_sentinel/services/runtime_versions.py
@@ -1,9 +1,12 @@
 """One bounded, thread-safe Worker observation belonging to this Core runtime."""
 
 from collections.abc import Callable
+from dataclasses import dataclass
 from datetime import datetime
 from threading import Lock
 from time import monotonic
+from typing import Literal
+from uuid import UUID
 
 from sentinel_contracts.runtime_versions import RuntimeVersion, VersionObservation
 from sentinel_contracts.time import floor_utc_millisecond, utc_now_ms
@@ -11,13 +14,27 @@
 from sentinel_contracts.worker_diagnostics import (
     BrokerRuntimeDiagnostics,
     OutboxObservation,
+    WorkerAutomationExecutionDiagnostics,
     WorkerDiagnostics,
+    WorkerExecutionDiagnostics,
     WorkerObservation,
 )
 
 
 def _timestamp(value: datetime | None) -> str | None:
     return None if value is None else value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
+
+
+@dataclass(frozen=True)
+class ScopedWorkerExecutionObservation:
+    observation: Literal["OBSERVED", "UNKNOWN"]
+    reason: str
+    broker_id: UUID | None = None
+    captured_at: datetime | None = None
+    received_at: str | None = None
+    source_runtime_instance_id: UUID | None = None
+    source_generation: int | None = None
+    row: WorkerAutomationExecutionDiagnostics | None = None
 
 
 class WorkerVersionStore:
@@ -41,6 +58,12 @@
         self._diagnostics: WorkerDiagnostics | None = None
         self._diagnostics_received_at: str | None = None
         self._diagnostics_monotonic = 0.0
+        self._execution_projection: WorkerExecutionDiagnostics | None = None
+        self._execution_runtime_instance_id: UUID | None = None
+        self._execution_generation: int | None = None
+        self._execution_broker_id: UUID | None = None
+        self._execution_received_at: str | None = None
+        self._execution_received_monotonic = 0.0
         self._completion_age_ms: int | None = None
         self._completion_monotonic = 0.0
         self._oldest_pending_age_ms: int | None = None
@@ -51,11 +74,30 @@
         metadata: RuntimeVersion,
         occurred_at: datetime,
         diagnostics: WorkerDiagnostics | None = None,
+        *,
+        source_runtime_instance_id: UUID | None = None,
+        source_generation: int | None = None,
+        source_broker_id: UUID | None = None,
     ) -> None:
         if metadata.environment != self._environment or metadata.access_mode != self._access_mode:
             return
         if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
             return
+        execution = None if diagnostics is None else diagnostics.execution_diagnostics
+        if execution is not None and (
+            source_runtime_instance_id is not None
+            and (
+                source_runtime_instance_id != metadata.instance_id
+                or source_generation is None
+                or source_generation <= 0
+                or source_broker_id is None
+                or (execution.broker_id is not None and execution.broker_id != source_broker_id)
+            )
+        ):
+            return
+        if execution is not None and source_runtime_instance_id is None and execution.observation == "OBSERVED":
+            execution = WorkerExecutionDiagnostics(reason="UNAUTHENTICATED_SOURCE")
+            diagnostics = diagnostics.model_copy(update={"execution_diagnostics": execution})
         timestamp = floor_utc_millisecond(occurred_at)
         with self._lock:
             now = floor_utc_millisecond(self._clock())
@@ -74,6 +116,12 @@
                 self._diagnostics_received_at = None
                 self._completion_age_ms = None
                 self._oldest_pending_age_ms = None
+                self._execution_projection = None
+                self._execution_runtime_instance_id = None
+                self._execution_generation = None
+                self._execution_broker_id = None
+                self._execution_received_at = None
+                self._execution_received_monotonic = 0.0
             if diagnostics is not None:
                 if previous is None or diagnostics.last_completed_at != previous.last_completed_at:
                     self._completion_age_ms = self._source_age(now, diagnostics.last_completed_at)
@@ -84,6 +132,12 @@
                 self._diagnostics = diagnostics
                 self._diagnostics_received_at = _timestamp(now)
                 self._diagnostics_monotonic = tick
+                self._execution_projection = execution
+                self._execution_runtime_instance_id = source_runtime_instance_id
+                self._execution_generation = source_generation
+                self._execution_broker_id = source_broker_id
+                self._execution_received_at = _timestamp(now) if execution is not None else None
+                self._execution_received_monotonic = tick if execution is not None else 0.0
             self._version = metadata
             self._occurred_at = timestamp
             self._received_at = _timestamp(now)
@@ -140,6 +194,113 @@
         with self._lock:
             tick = self._monotonic()
             return self._version_snapshot(tick), self._worker_snapshot(tick)
+
+    def execution_snapshot(
+        self,
+        *,
+        broker_id: UUID,
+        automation_id: UUID,
+        account_id: str,
+        instrument_id: str,
+        fact_instrument_id: UUID,
+        lot_size: int,
+        owner_runtime_instance_id: UUID | None,
+        owner_generation: int | None,
+    ) -> ScopedWorkerExecutionObservation:
+        with self._lock:
+            projection = self._execution_projection
+            source_runtime = self._execution_runtime_instance_id
+            source_generation = self._execution_generation
+            source_broker = self._execution_broker_id
+            received_at = self._execution_received_at
+            if projection is None:
+                return ScopedWorkerExecutionObservation("UNKNOWN", "NOT_OBSERVED", broker_id=broker_id)
+            if projection.reason == "UNAUTHENTICATED_SOURCE":
+                return ScopedWorkerExecutionObservation(
+                    "UNKNOWN",
+                    "UNAUTHENTICATED_SOURCE",
+                    broker_id=broker_id,
+                    captured_at=projection.captured_at,
+                    received_at=received_at,
+                )
+            if (
+                owner_runtime_instance_id is None
+                or owner_generation is None
+                or source_runtime != owner_runtime_instance_id
+                or source_generation != owner_generation
+            ):
+                return ScopedWorkerExecutionObservation(
+                    "UNKNOWN",
+                    "OWNER_CHANGED",
+                    broker_id=broker_id,
+                    captured_at=projection.captured_at,
+                    received_at=received_at,
+                    source_runtime_instance_id=source_runtime,
+                    source_generation=source_generation,
+                )
+            if source_broker != broker_id or projection.broker_id not in {None, broker_id}:
+                return ScopedWorkerExecutionObservation(
+                    "UNKNOWN",
+                    "INVALID_PROJECTION",
+                    broker_id=broker_id,
+                    received_at=received_at,
+                    source_runtime_instance_id=source_runtime,
+                    source_generation=source_generation,
+                )
+            if projection.observation == "UNKNOWN":
+                return ScopedWorkerExecutionObservation(
+                    "UNKNOWN",
+                    projection.reason,
+                    broker_id=broker_id,
+                    received_at=received_at,
+                    source_runtime_instance_id=source_runtime,
+                    source_generation=source_generation,
+                )
+            now = floor_utc_millisecond(self._clock())
+            age_ms = (now - projection.captured_at).total_seconds() * 1000
+            receipt_age_ms = (self._monotonic() - self._execution_received_monotonic) * 1000
+            if age_ms < -5000 or age_ms >= 30_000 or receipt_age_ms >= 30_000:
+                return ScopedWorkerExecutionObservation(
+                    "UNKNOWN",
+                    "STALE",
+                    broker_id=broker_id,
+                    captured_at=projection.captured_at,
+                    received_at=received_at,
+                    source_runtime_instance_id=source_runtime,
+                    source_generation=source_generation,
+                )
+            row = next(
+                (
+                    item
+                    for item in projection.rows
+                    if item.automation_id == automation_id
+                    and item.account_id == account_id
+                    and item.external_instrument_id == instrument_id
+                    and item.fact_instrument_id == fact_instrument_id
+                    and item.lot_size == lot_size
+                ),
+                None,
+            )
+            if row is None:
+                return ScopedWorkerExecutionObservation(
+                    "UNKNOWN",
+                    "INVALID_PROJECTION",
+                    broker_id=broker_id,
+                    captured_at=projection.captured_at,
+                    received_at=received_at,
+                    source_runtime_instance_id=source_runtime,
+                    source_generation=source_generation,
+                )
+            return ScopedWorkerExecutionObservation(
+                "OBSERVED",
+                "OBSERVED",
+                broker_id=broker_id,
+                captured_at=projection.captured_at,
+                received_at=received_at,
+                source_runtime_instance_id=source_runtime,
+                source_generation=source_generation,
+                row=row,
+            )
 
     def _version_snapshot(self, tick: float) -> VersionObservation:
         if self._version is None:
```

## tests/contracts/test_execution_diagnostics.py

```diff
--- baseline/tests/contracts/test_execution_diagnostics.py
+++ tests/contracts/test_execution_diagnostics.py
@@ -0,0 +1,139 @@
+from datetime import UTC, datetime
+from decimal import Decimal
+from uuid import UUID
+
+import pytest
+from pydantic import ValidationError
+
+from sentinel_contracts.worker_diagnostics import (
+    WorkerAutomationExecutionDiagnostics,
+    WorkerDiagnostics,
+    WorkerExecutionDiagnostics,
+    WorkerIntentDiagnostics,
+)
+
+NOW = datetime(2026, 10, 20, 12, 0, 0, 123456, tzinfo=UTC)
+AUTOMATION_ID = UUID("00000000-0000-4000-8000-000000000101")
+BROKER_ID = UUID("00000000-0000-4000-8000-000000000102")
+INTENT_ID = UUID("00000000-0000-4000-8000-000000000103")
+
+
+def intent(*, intent_id: UUID = INTENT_ID) -> WorkerIntentDiagnostics:
+    return WorkerIntentDiagnostics(
+        intent_id=intent_id,
+        decision_id=UUID("00000000-0000-4000-8000-000000000104"),
+        process_id=None,
+        active=True,
+        state="UNCERTAIN",
+        side="BUY",
+        external_order_id=None,
+        quantity_lots=1,
+        limit_price=Decimal("10.5"),
+        requested_amount=Decimal("10.5"),
+        executed_lots=0,
+        executed_amount=Decimal("0"),
+        executed_price=Decimal("0"),
+        estimated_commission=Decimal("0.1"),
+        executed_commission=Decimal("0"),
+        currency="RUB",
+        fact_execution_id=None,
+        execution_facts_emitted_at=None,
+        origin_runtime_instance_id=None,
+        origin_generation=None,
+        created_at=NOW,
+        updated_at=NOW,
+        dispatch_started_at=None,
+        broker_responded_at=None,
+        executed_at=None,
+        terminal_at=None,
+    )
+
+
+def test_execution_projection_is_bounded_and_normalizes_timestamps() -> None:
+    row = WorkerAutomationExecutionDiagnostics(
+        automation_id=AUTOMATION_ID,
+        account_id="account-1",
+        external_instrument_id="instrument-1",
+        fact_instrument_id=UUID("00000000-0000-4000-8000-000000000105"),
+        lot_size=10,
+        pending_fact_count=2,
+        failed_fact_count=0,
+        intents=(intent(),),
+        history_truncated=False,
+    )
+    projection = WorkerExecutionDiagnostics(
+        observation="OBSERVED",
+        reason="OBSERVED",
+        broker_id=BROKER_ID,
+        captured_at=NOW,
+        rows=(row,),
+        truncated=False,
+    )
+
+    assert projection.captured_at == NOW.replace(microsecond=123000)
+    assert projection.rows[0].intents[0].created_at == NOW.replace(microsecond=123000)
+    assert "strategy_snapshot" not in projection.model_dump(mode="json")
+    assert "safe_message" not in projection.model_dump(mode="json")
+
+
+def test_unknown_projection_carries_no_financial_rows() -> None:
+    projection = WorkerExecutionDiagnostics(
+        observation="UNKNOWN",
+        reason="BUDGET_EXCEEDED",
+        broker_id=BROKER_ID,
+        rows=(),
+    )
+
+    assert projection.captured_at is None
+    assert projection.rows == ()
+    with pytest.raises(ValidationError):
+        WorkerExecutionDiagnostics(
+            observation="UNKNOWN",
+            reason="READ_FAILED",
+            rows=(
+                WorkerAutomationExecutionDiagnostics(
+                    automation_id=AUTOMATION_ID,
+                    account_id="account-1",
+                    external_instrument_id="instrument-1",
+                    fact_instrument_id=None,
+                    lot_size=1,
+                    pending_fact_count=0,
+                    failed_fact_count=0,
+                ),
+            ),
+        )
+
+
+def test_worker_diagnostics_keeps_execution_projection_optional() -> None:
+    base = {
+        "completed_iterations": 0,
+        "last_completed_at": None,
+        "last_finished_at": NOW,
+        "last_result": "ERROR",
+        "error_code": "ITERATION_FAILED",
+        "outbox": {"reason": "READ_FAILED"},
+    }
+
+    assert WorkerDiagnostics.model_validate(base).execution_diagnostics is None
+    assert (
+        WorkerDiagnostics.model_validate(
+            {**base, "execution_diagnostics": {"observation": "UNKNOWN", "reason": "READ_FAILED"}}
+        ).execution_diagnostics.reason
+        == "READ_FAILED"
+    )
+
+
+def test_execution_projection_rejects_sixth_intent_and_hides_raw_fields() -> None:
+    with pytest.raises(ValidationError):
+        WorkerAutomationExecutionDiagnostics(
+            automation_id=AUTOMATION_ID,
+            account_id="account-1",
+            external_instrument_id="instrument-1",
+            fact_instrument_id=None,
+            lot_size=1,
+            pending_fact_count=0,
+            failed_fact_count=0,
+            intents=tuple(intent(intent_id=UUID(int=index + 1)) for index in range(6)),
+            history_truncated=True,
+            raw_payload={"secret": "must-not-pass"},
+        )
```

## tests/trading_automaton/storage/test_execution_diagnostics.py

```diff
--- baseline/tests/trading_automaton/storage/test_execution_diagnostics.py
+++ tests/trading_automaton/storage/test_execution_diagnostics.py
@@ -0,0 +1,326 @@
+from datetime import UTC, datetime
+from decimal import Decimal
+from pathlib import Path
+from time import monotonic
+from uuid import UUID
+
+import pytest
+from sqlalchemy import create_engine, event, text
+from sqlalchemy.orm import sessionmaker
+
+import trading_automaton.storage.execution_diagnostics as diagnostics
+from sentinel_contracts.broker_execution import BrokerOrderStatus
+from sentinel_contracts.worker_diagnostics import WorkerExecutionDiagnostics
+from tests.trading_automaton.command_factory import command
+from trading_automaton.storage.execution_diagnostics import WorkerExecutionDiagnosticsReader
+from trading_automaton.storage.models import Base, LocalIntentModel
+from trading_automaton.storage.repository import LocalAutomationRepository
+
+NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
+BROKER_ID = UUID("00000000-0000-4000-8000-000000000102")
+
+
+def worker_database(path: Path):
+    engine = create_engine(f"sqlite:///{path}")
+    Base.metadata.create_all(engine)
+    repository = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
+    value = command(broker=str(BROKER_ID))
+    repository.cache_command(value)
+    engine.dispose()
+    return value
+
+
+def test_reader_does_not_checkout_the_main_pool(tmp_path: Path) -> None:
+    path = tmp_path / "worker.sqlite"
+    value = worker_database(path)
+    main = create_engine(f"sqlite:///{path}", pool_size=1, max_overflow=0)
+    try:
+        with main.connect():
+            started = monotonic()
+            result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(value.broker_id, NOW)
+            elapsed = monotonic() - started
+
+        assert result.observation == "OBSERVED"
+        assert result.rows[0].automation_id == value.automation_id
+        assert elapsed < 0.1
+    finally:
+        main.dispose()
+
+
+def test_reader_keeps_active_intent_first_and_truncates_terminal_history(tmp_path: Path) -> None:
+    path = tmp_path / "history.sqlite"
+    value = worker_database(path)
+    engine = create_engine(f"sqlite:///{path}")
+    terminal_states = (BrokerOrderStatus.CANCELLED,) * 5
+    with engine.begin() as connection:
+        states = (BrokerOrderStatus.FAILED, BrokerOrderStatus.FILLED, *terminal_states)
+        for index, state in enumerate(states):
+            connection.execute(
+                LocalIntentModel.__table__.insert().values(
+                    idempotency_key=str(UUID(int=index + 1)),
+                    fact_execution_id=None,
+                    position_cycle_id=None,
+                    automation_id=str(value.automation_id),
+                    kind="BUY_MORE",
+                    side="BUY",
+                    state=state,
+                    quantity_lots=1,
+                    limit_price=Decimal("10"),
+                    broker_order_id=None,
+                    requested_amount=Decimal("10"),
+                    executed_amount=Decimal("0"),
+                    estimated_commission=Decimal("0"),
+                    executed_commission=Decimal("0"),
+                    executed_lots=0,
+                    executed_price=Decimal("7.5"),
+                    execution_currency="RUB",
+                    executed_at=None,
+                    dispatch_started_at=None,
+                    broker_responded_at=None,
+                    terminal_at=None if index == 0 else NOW,
+                    execution_facts_emitted_at=None,
+                    strategy_snapshot={
+                        "admission_origin": {
+                            "runtime_instance_id": "00000000-0000-4000-8000-000000000201",
+                            "generation": 3,
+                        },
+                        "private_strategy_value": "must-not-leak",
+                    },
+                    created_at=NOW,
+                    updated_at=NOW.replace(second=index),
+                )
+            )
+    engine.dispose()
+
+    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}", environment="PROD").read(value.broker_id, NOW)
+
+    assert result.observation == "OBSERVED", result
+    intents = result.rows[0].intents
+    assert len(intents) == 5
+    assert intents[0].active is True
+    assert intents[0].limit_price == Decimal("10")
+    assert intents[0].executed_price == Decimal("7.5")
+    assert intents[0].origin_generation == 3
+    assert intents[0].origin_runtime_instance_id == UUID("00000000-0000-4000-8000-000000000201")
+    assert result.rows[0].history_truncated is True
+    assert "private_strategy_value" not in result.model_dump(mode="json").__str__()
+
+    test_result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}", environment="TEST").read(value.broker_id, NOW)
+    assert test_result.rows[0].intents[0].active is True
+    filled = next(intent for intent in test_result.rows[0].intents if intent.state == "FILLED")
+    assert filled.active is True
+
+
+@pytest.mark.parametrize(
+    ("environment", "state", "terminal_at", "execution_emitted_at", "newer_count", "expected_active"),
+    [
+        ("PROD", BrokerOrderStatus.CANCELLED, None, None, 5, True),
+        ("PROD", BrokerOrderStatus.REJECTED, None, None, 4, True),
+        ("PROD", BrokerOrderStatus.EXPIRED, None, None, 4, True),
+        ("TEST", BrokerOrderStatus.CANCELLED, None, None, 4, False),
+        ("TEST", BrokerOrderStatus.REJECTED, None, None, 4, False),
+        ("TEST", BrokerOrderStatus.EXPIRED, None, None, 4, False),
+        ("TEST", BrokerOrderStatus.FILLED, NOW, None, 4, True),
+        ("TEST", BrokerOrderStatus.FAILED, NOW, None, 4, False),
+    ],
+)
+def test_environment_active_policy_prioritizes_only_unfinalized_intents(
+    tmp_path: Path,
+    environment: str,
+    state: BrokerOrderStatus,
+    terminal_at: datetime | None,
+    execution_emitted_at: datetime | None,
+    newer_count: int,
+    expected_active: bool,
+) -> None:
+    path = tmp_path / f"active-policy-{environment}-{state.value}.sqlite"
+    value = worker_database(path)
+    engine = create_engine(f"sqlite:///{path}")
+    with engine.begin() as connection:
+        connection.execute(
+            LocalIntentModel.__table__.insert().values(
+                idempotency_key=str(UUID(int=1000)),
+                fact_execution_id=None,
+                position_cycle_id=None,
+                automation_id=str(value.automation_id),
+                kind="BUY_MORE",
+                side="BUY",
+                state=state,
+                quantity_lots=1,
+                limit_price=Decimal("10"),
+                broker_order_id=None,
+                requested_amount=Decimal("10"),
+                executed_amount=Decimal("0"),
+                estimated_commission=Decimal("0"),
+                executed_commission=Decimal("0"),
+                executed_lots=0,
+                executed_price=Decimal("0"),
+                execution_currency="RUB",
+                executed_at=None,
+                dispatch_started_at=None,
+                broker_responded_at=None,
+                terminal_at=terminal_at,
+                execution_facts_emitted_at=execution_emitted_at,
+                strategy_snapshot={},
+                created_at=NOW.replace(minute=0),
+                updated_at=NOW.replace(minute=0),
+            )
+        )
+        for index in range(newer_count):
+            connection.execute(
+                LocalIntentModel.__table__.insert().values(
+                    idempotency_key=str(UUID(int=1100 + index)),
+                    fact_execution_id=None,
+                    position_cycle_id=None,
+                    automation_id=str(value.automation_id),
+                    kind="BUY_MORE",
+                    side="BUY",
+                    state=BrokerOrderStatus.CANCELLED,
+                    quantity_lots=1,
+                    limit_price=Decimal("10"),
+                    broker_order_id=None,
+                    requested_amount=Decimal("10"),
+                    executed_amount=Decimal("0"),
+                    estimated_commission=Decimal("0"),
+                    executed_commission=Decimal("0"),
+                    executed_lots=0,
+                    executed_price=Decimal("0"),
+                    execution_currency="RUB",
+                    executed_at=None,
+                    dispatch_started_at=None,
+                    broker_responded_at=None,
+                    terminal_at=NOW,
+                    execution_facts_emitted_at=NOW,
+                    strategy_snapshot={},
+                    created_at=NOW,
+                    updated_at=NOW.replace(second=index + 1),
+                )
+            )
+    engine.dispose()
+
+    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}", environment=environment).read(value.broker_id, NOW)
+
+    assert result.observation == "OBSERVED", result
+    target = next(intent for intent in result.rows[0].intents if intent.intent_id == UUID(int=1000))
+    assert target.active is expected_active
+    if expected_active and newer_count == 5:
+        assert result.rows[0].intents[0].intent_id == UUID(int=1000)
+
+
+def test_reader_bounds_automation_rows_at_one_hundred(tmp_path: Path) -> None:
+    path = tmp_path / "many.sqlite"
+    engine = create_engine(f"sqlite:///{path}")
+    Base.metadata.create_all(engine)
+    repository = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
+    value = None
+    for index in range(101):
+        value = command(automation=f"many-{index}", broker=str(BROKER_ID))
+        repository.cache_command(value)
+    engine.dispose()
+
+    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(value.broker_id, NOW)
+
+    assert result.observation == "OBSERVED"
+    assert len(result.rows) == 100
+    assert result.truncated is True
+
+
+def test_expensive_query_is_interrupted_without_partial_projection(tmp_path: Path, monkeypatch) -> None:
+    path = tmp_path / "expensive.sqlite"
+    value = worker_database(path)
+    monkeypatch.setattr(
+        diagnostics,
+        "_INTENTS",
+        text(
+            "WITH RECURSIVE counter(value) AS ("
+            "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 1000000000"
+            ") SELECT value FROM counter"
+        ),
+    )
+    started = monotonic()
+
+    result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(value.broker_id, NOW)
+
+    assert result.observation == "UNKNOWN"
+    assert result.reason == "BUDGET_EXCEEDED"
+    assert result.rows == ()
+    assert monotonic() - started < 0.25
+
+
+def test_connections_are_closed_after_timeout_and_repeated_success(tmp_path: Path, monkeypatch) -> None:
+    path = tmp_path / "cleanup.sqlite"
+    value = worker_database(path)
+    opened = 0
+    closed = 0
+    create_engine = diagnostics.create_engine
+
+    def tracked_engine(*args, **kwargs):
+        engine = create_engine(*args, **kwargs)
+
+        def on_connect(_dbapi_connection, _record):
+            nonlocal opened
+            opened += 1
+
+        def on_close(_dbapi_connection, _record):
+            nonlocal closed
+            closed += 1
+
+        event.listen(engine, "connect", on_connect)
+        event.listen(engine, "close", on_close)
+        return engine
+
+    monkeypatch.setattr(diagnostics, "create_engine", tracked_engine)
+    reader = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}")
+    assert reader.read(value.broker_id, NOW).observation == "OBSERVED"
+    with monkeypatch.context() as timeout_patch:
+        timeout_patch.setattr(
+            diagnostics,
+            "_INTENTS",
+            text(
+                "WITH RECURSIVE counter(value) AS ("
+                "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 1000000000"
+                ") SELECT value FROM counter"
+            ),
+        )
+        timed_out = reader.read(value.broker_id, NOW)
+    assert timed_out.observation == "UNKNOWN"
+    assert timed_out.reason == "BUDGET_EXCEEDED"
+    assert reader.read(value.broker_id, NOW).observation == "OBSERVED"
+
+    assert opened == closed == 3
+
+
+def test_missing_and_memory_sources_fail_without_creating_a_file(tmp_path: Path) -> None:
+    path = tmp_path / "must-not-exist.sqlite"
+
+    missing = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(BROKER_ID, NOW)
+    memory = WorkerExecutionDiagnosticsReader("sqlite:///:memory:").read(BROKER_ID, NOW)
+
+    assert missing.observation == memory.observation == "UNKNOWN"
+    assert missing.reason == memory.reason == "UNSUPPORTED_SOURCE"
+    assert not path.exists()
+
+
+def test_exclusive_writer_lock_returns_unknown_within_budget(tmp_path: Path) -> None:
+    path = tmp_path / "locked.sqlite"
+    value = worker_database(path)
+    writer = create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.05})
+    try:
+        with writer.connect() as connection:
+            connection.exec_driver_sql("PRAGMA journal_mode=DELETE")
+            connection.exec_driver_sql("BEGIN EXCLUSIVE")
+            started = monotonic()
+            result = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}").read(value.broker_id, NOW)
+            elapsed = monotonic() - started
+
+        assert result.observation == "UNKNOWN"
+        assert result.reason == "BUDGET_EXCEEDED"
+        assert result.rows == ()
+        assert elapsed < 0.2
+    finally:
+        writer.dispose()
+
+
+def test_reader_result_type_is_the_shared_safe_projection() -> None:
+    assert WorkerExecutionDiagnosticsReader
+    assert WorkerExecutionDiagnostics
```

## tests/trading_automaton/runtime/test_execution_diagnostics_budget.py

```diff
--- baseline/tests/trading_automaton/runtime/test_execution_diagnostics_budget.py
+++ tests/trading_automaton/runtime/test_execution_diagnostics_budget.py
@@ -0,0 +1,65 @@
+import asyncio
+from datetime import UTC, datetime
+from pathlib import Path
+from time import monotonic
+from uuid import UUID
+
+from sqlalchemy import create_engine
+from sqlalchemy.orm import sessionmaker
+
+from tests.trading_automaton.command_factory import command
+from trading_automaton.runtime.streaming_coordinator import StreamingRuntimeCoordinator
+from trading_automaton.storage.execution_diagnostics import WorkerExecutionDiagnosticsReader
+from trading_automaton.storage.models import Base
+from trading_automaton.storage.repository import LocalAutomationRepository
+
+NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
+BROKER_ID = UUID("00000000-0000-4000-8000-000000000302")
+
+
+def test_sqlite_lock_still_delivers_heartbeat_and_allows_coordinator_close(tmp_path: Path) -> None:
+    path = tmp_path / "worker.sqlite"
+    engine = create_engine(f"sqlite:///{path}")
+    Base.metadata.create_all(engine)
+    value = command(broker=str(BROKER_ID))
+    LocalAutomationRepository(sessionmaker(engine)).cache_command(value)
+    engine.dispose()
+
+    reader = WorkerExecutionDiagnosticsReader(f"sqlite:///{path}")
+    heartbeats = []
+    coordinator = StreamingRuntimeCoordinator(
+        object(),
+        type(
+            "Core",
+            (),
+            {"heartbeat": lambda _self, _worker, _at, diagnostics: heartbeats.append(diagnostics)},
+        )(),
+        lambda _: None,
+        worker_id="worker",
+        now=lambda: NOW,
+        execution_diagnostics=lambda captured_at: reader.read(BROKER_ID, captured_at),
+    )
+    coordinator._last_finished_at = NOW
+    coordinator._last_result = "OUTBOX_BLOCKED"
+
+    lock_engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.05})
+
+    async def send_and_close():
+        with lock_engine.connect() as connection:
+            connection.exec_driver_sql("PRAGMA journal_mode=DELETE")
+            connection.exec_driver_sql("BEGIN EXCLUSIVE")
+            started = monotonic()
+            coordinator._schedule_heartbeat()
+            await asyncio.wait_for(coordinator.close(), timeout=0.3)
+            elapsed = monotonic() - started
+        return elapsed
+
+    try:
+        elapsed = asyncio.run(send_and_close())
+    finally:
+        lock_engine.dispose()
+
+    assert elapsed < 0.3
+    assert len(heartbeats) == 1
+    assert heartbeats[0].execution_diagnostics.observation == "UNKNOWN"
+    assert heartbeats[0].execution_diagnostics.reason == "BUDGET_EXCEEDED"
```

## tests/trading_automaton/adapters/test_core_client.py

```diff
--- baseline/tests/trading_automaton/adapters/test_core_client.py
+++ tests/trading_automaton/adapters/test_core_client.py
@@ -24,6 +24,7 @@
     FactBatchResult,
 )
 from sentinel_contracts.version import SERVICE_VERSION
+from sentinel_contracts.worker_diagnostics import WorkerDiagnostics, WorkerExecutionDiagnostics
 from tests.contracts.trading_facts_helpers import all_envelopes
 from tests.services.test_runtime_versions import NOW, progress
 from trading_automaton.adapters.core_client import CoreClient
@@ -115,6 +116,72 @@
     assert attempts == 1
 
 
+@pytest.mark.parametrize("marker", [None, "v2", ""])
+def test_heartbeat_omits_execution_diagnostics_without_v1_capability(marker) -> None:
+    heartbeat_bodies = []
+
+    def handler(request: httpx.Request) -> httpx.Response:
+        if request.url.path == "/internal/runtime":
+            config = {"environment": "TEST", "access_mode": "READ_ONLY"}
+            if marker is not None:
+                config["execution_diagnostics"] = marker
+            return httpx.Response(200, json=config)
+        if request.url.path.endswith("/heartbeats"):
+            heartbeat_bodies.append(json.loads(request.content))
+            return httpx.Response(200, json={})
+        raise AssertionError(f"Unexpected route: {request.url.path}")
+
+    diagnostics = WorkerDiagnostics(
+        completed_iterations=0,
+        last_completed_at=None,
+        last_finished_at=datetime(2026, 10, 20, tzinfo=UTC),
+        last_result="OUTBOX_BLOCKED",
+        outbox={"reason": "READ_FAILED"},
+        execution_diagnostics=WorkerExecutionDiagnostics(reason="UNCONFIGURED_BROKER"),
+    )
+    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core:8000") as http:
+        CoreClient(
+            http,
+            application_environment="TEST",
+            access_mode="READ_ONLY",
+        ).heartbeat("worker", datetime(2026, 10, 20, tzinfo=UTC), diagnostics)
+
+    assert len(heartbeat_bodies) == 1
+    assert "execution_diagnostics" not in heartbeat_bodies[0]["worker_diagnostics"]
+
+
+def test_heartbeat_sends_execution_diagnostics_when_core_advertises_v1() -> None:
+    heartbeat_bodies = []
+
+    def handler(request: httpx.Request) -> httpx.Response:
+        if request.url.path == "/internal/runtime":
+            return httpx.Response(
+                200,
+                json={"environment": "TEST", "access_mode": "READ_ONLY", "execution_diagnostics": "v1"},
+            )
+        if request.url.path.endswith("/heartbeats"):
+            heartbeat_bodies.append(json.loads(request.content))
+            return httpx.Response(200, json={})
+        raise AssertionError(f"Unexpected route: {request.url.path}")
+
+    diagnostics = WorkerDiagnostics(
+        completed_iterations=0,
+        last_completed_at=None,
+        last_finished_at=datetime(2026, 10, 20, tzinfo=UTC),
+        last_result="OUTBOX_BLOCKED",
+        outbox={"reason": "READ_FAILED"},
+        execution_diagnostics=WorkerExecutionDiagnostics(reason="UNCONFIGURED_BROKER"),
+    )
+    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core:8000") as http:
+        CoreClient(
+            http,
+            application_environment="TEST",
+            access_mode="READ_ONLY",
+        ).heartbeat("worker", datetime(2026, 10, 20, tzinfo=UTC), diagnostics)
+
+    assert heartbeat_bodies[0]["worker_diagnostics"]["execution_diagnostics"]["reason"] == "UNCONFIGURED_BROKER"
+
+
 def test_keeps_required_heartbeat_and_broker_connection_routes() -> None:
     requests: list[httpx.Request] = []
 
```

## tests/api/test_runtime_versions.py

```diff
--- baseline/tests/api/test_runtime_versions.py
+++ tests/api/test_runtime_versions.py
@@ -10,7 +10,12 @@
 from moex_sentinel.api.app import create_app
 from moex_sentinel.services.runtime_versions import WorkerVersionStore
 from sentinel_contracts.runtime_versions import RuntimeVersion
-from sentinel_contracts.worker_diagnostics import CommandSyncDiagnostics, CommandSyncRow
+from sentinel_contracts.worker_diagnostics import (
+    CommandSyncDiagnostics,
+    CommandSyncRow,
+    WorkerAutomationExecutionDiagnostics,
+    WorkerExecutionDiagnostics,
+)
 from tests.services.test_runtime_versions import progress
 from trading_automaton.adapters.core_client import CoreClient
 
@@ -172,6 +177,146 @@
     assert worker["broker_runtime"]["status"] == "UNKNOWN"
     assert worker["broker_runtime"]["decision_eligibility"] == "UNKNOWN"
     assert worker["command_sync"] is None
+
+
+def test_invalid_optional_execution_projection_does_not_reject_control_heartbeat(tmp_path):
+    now = datetime.now(UTC)
+    payload = {
+        "worker_id": "worker",
+        "occurred_at": now.isoformat(),
+        "runtime_version": {
+            "version": "1.2.3",
+            "instance_id": str(uuid4()),
+            "environment": "TEST",
+            "access_mode": "READ_ONLY",
+        },
+        "worker_diagnostics": progress(completed=now, finished=now).model_dump(mode="json"),
+    }
+    payload["worker_diagnostics"]["execution_diagnostics"] = {
+        "observation": "OBSERVED",
+        "reason": "OBSERVED",
+        "broker_id": str(uuid4()),
+        "captured_at": now.isoformat(),
+        "rows": [{"account_id": "secret", "raw_payload": "must-not-be-stored"}],
+    }
+
+    application = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'invalid-execution.db'}")
+    with TestClient(application) as client:
+        response = client.post("/internal/automaton/heartbeats", json=payload)
+        status = client.get("/api/diagnostics/status").json()
+
+    assert response.status_code == 200
+    assert status["worker"]["reason"] == "CONTROL_PROGRESS"
+    assert "execution_diagnostics" not in status["worker"]
+    assert "must-not-be-stored" not in response.text + str(status)
+    assert client.app.state.worker_versions._execution_projection.reason == "INVALID_PROJECTION"
+
+
+def test_unauthenticated_test_execution_projection_is_not_publicly_exposed(tmp_path):
+    now = datetime.now(UTC)
+    broker_id, automation_id, fact_instrument_id = uuid4(), uuid4(), uuid4()
+    diagnostics = progress(completed=now, finished=now).model_copy(
+        update={
+            "execution_diagnostics": WorkerExecutionDiagnostics(
+                observation="OBSERVED",
+                reason="OBSERVED",
+                broker_id=broker_id,
+                captured_at=now,
+                rows=(
+                    WorkerAutomationExecutionDiagnostics(
+                        automation_id=automation_id,
+                        account_id="test-account",
+                        external_instrument_id="test-instrument",
+                        fact_instrument_id=fact_instrument_id,
+                        lot_size=1,
+                        pending_fact_count=2,
+                        failed_fact_count=0,
+                    ),
+                ),
+            )
+        }
+    )
+    payload = {
+        "worker_id": "worker",
+        "occurred_at": now.isoformat(),
+        "runtime_version": {
+            "version": "1.2.3",
+            "instance_id": str(uuid4()),
+            "environment": "TEST",
+            "access_mode": "READ_ONLY",
+        },
+        "worker_diagnostics": diagnostics.model_dump(mode="json"),
+    }
+    application = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'test-execution.db'}")
+    with TestClient(application) as client:
+        assert client.post("/internal/automaton/heartbeats", json=payload).status_code == 200
+        status = client.get("/api/diagnostics/status").json()
+        stored_execution = client.app.state.worker_versions._execution_projection
+        scoped = client.app.state.worker_versions.execution_snapshot(
+            broker_id=broker_id,
+            automation_id=automation_id,
+            account_id="test-account",
+            instrument_id="test-instrument",
+            fact_instrument_id=fact_instrument_id,
+            lot_size=1,
+            owner_runtime_instance_id=None,
+            owner_generation=None,
+        )
+
+    assert status["worker"]["reason"] == "CONTROL_PROGRESS"
+    assert "execution_diagnostics" not in status["worker"]
+    assert stored_execution.observation == "UNKNOWN"
+    assert stored_execution.reason == "UNAUTHENTICATED_SOURCE"
+    assert stored_execution.rows == ()
+    assert scoped.reason == "UNAUTHENTICATED_SOURCE"
+
+
+@pytest.mark.parametrize(
+    "execution_projection",
+    [
+        None,
+        {
+            "observation": "OBSERVED",
+            "reason": "OBSERVED",
+            "broker_id": str(uuid4()),
+            "captured_at": datetime.now(UTC).isoformat(),
+            "rows": [{}] * 101,
+        },
+        {
+            "observation": "OBSERVED",
+            "reason": "OBSERVED",
+            "broker_id": str(uuid4()),
+            "captured_at": datetime.now(UTC).isoformat(),
+            "rows": [{"intents": [{}] * 6}],
+        },
+    ],
+)
+def test_optional_execution_projection_null_and_oversize_prevalidation_preserve_control(tmp_path, execution_projection):
+    now = datetime.now(UTC)
+    diagnostics = progress(completed=now, finished=now).model_dump(mode="json")
+    diagnostics["execution_diagnostics"] = execution_projection
+    payload = {
+        "worker_id": "worker",
+        "occurred_at": now.isoformat(),
+        "runtime_version": {
+            "version": "1.2.3",
+            "instance_id": str(uuid4()),
+            "environment": "TEST",
+            "access_mode": "READ_ONLY",
+        },
+        "worker_diagnostics": diagnostics,
+    }
+    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'prevalidate.db'}")) as client:
+        response = client.post("/internal/automaton/heartbeats", json=payload)
+        stored = client.app.state.worker_versions._execution_projection
+
+    assert response.status_code == 200
+    assert response.json()["worker_id"] == "worker"
+    if execution_projection is None:
+        assert stored is None
+    else:
+        assert stored.reason == "INVALID_PROJECTION"
+        assert stored.rows == ()
 
 
 def test_worker_command_sync_projection_flows_through_existing_heartbeat(tmp_path):
```

## tests/api/test_internal_runtime.py

```diff
--- baseline/tests/api/test_internal_runtime.py
+++ tests/api/test_internal_runtime.py
@@ -18,9 +18,12 @@
     with TestClient(application) as client:
         response = client.get("/internal/runtime")
         assert response.status_code == 200
-        assert (
-            response.json() == client.get("/api/runtime").json() == {"environment": "TEST", "access_mode": "READ_ONLY"}
-        )
+        assert response.json() == {
+            "environment": "TEST",
+            "access_mode": "READ_ONLY",
+            "execution_diagnostics": "v1",
+        }
+        assert client.get("/api/runtime").json() == {"environment": "TEST", "access_mode": "READ_ONLY"}
 
 
 @pytest.mark.parametrize("state", [UserBrokerState.ACTIVE, UserBrokerState.DISABLED])
```

## tests/services/test_runtime_versions.py

```diff
--- baseline/tests/services/test_runtime_versions.py
+++ tests/services/test_runtime_versions.py
@@ -7,7 +7,13 @@
 
 from moex_sentinel.services.runtime_versions import WorkerVersionStore
 from sentinel_contracts.runtime_versions import RuntimeVersion
-from sentinel_contracts.worker_diagnostics import BrokerRuntimeDiagnostics, OutboxDiagnostics, WorkerDiagnostics
+from sentinel_contracts.worker_diagnostics import (
+    BrokerRuntimeDiagnostics,
+    OutboxDiagnostics,
+    WorkerAutomationExecutionDiagnostics,
+    WorkerDiagnostics,
+    WorkerExecutionDiagnostics,
+)
 
 NOW = datetime(2026, 10, 3, 12, 0, 0, 123000, tzinfo=UTC)
 
@@ -180,6 +186,171 @@
     assert observed.completed_iterations is None
 
 
+def test_scoped_execution_snapshot_requires_current_owner_and_exact_row_bindings():
+    source, _ = store()
+    identity = metadata()
+    broker_id = uuid4()
+    automation_id = uuid4()
+    row = WorkerAutomationExecutionDiagnostics(
+        automation_id=automation_id,
+        account_id="account-1",
+        external_instrument_id="instrument-1",
+        fact_instrument_id=uuid4(),
+        lot_size=10,
+        pending_fact_count=1,
+        failed_fact_count=0,
+    )
+    diagnostics = progress().model_copy(
+        update={
+            "execution_diagnostics": WorkerExecutionDiagnostics(
+                observation="OBSERVED",
+                reason="OBSERVED",
+                broker_id=broker_id,
+                captured_at=NOW,
+                rows=(row,),
+            )
+        }
+    )
+    source.observe(
+        identity,
+        NOW,
+        diagnostics,
+        source_runtime_instance_id=identity.instance_id,
+        source_generation=3,
+        source_broker_id=broker_id,
+    )
+
+    accepted = source.execution_snapshot(
+        broker_id=broker_id,
+        automation_id=automation_id,
+        account_id="account-1",
+        instrument_id="instrument-1",
+        fact_instrument_id=row.fact_instrument_id,
+        lot_size=row.lot_size,
+        owner_runtime_instance_id=identity.instance_id,
+        owner_generation=3,
+    )
+    takeover = source.execution_snapshot(
+        broker_id=broker_id,
+        automation_id=automation_id,
+        account_id="account-1",
+        instrument_id="instrument-1",
+        fact_instrument_id=row.fact_instrument_id,
+        lot_size=row.lot_size,
+        owner_runtime_instance_id=identity.instance_id,
+        owner_generation=4,
+    )
+    cross_account = source.execution_snapshot(
+        broker_id=broker_id,
+        automation_id=automation_id,
+        account_id="other-account",
+        instrument_id="instrument-1",
+        fact_instrument_id=row.fact_instrument_id,
+        lot_size=row.lot_size,
+        owner_runtime_instance_id=identity.instance_id,
+        owner_generation=3,
+    )
+    wrong_catalog_id = source.execution_snapshot(
+        broker_id=broker_id,
+        automation_id=automation_id,
+        account_id="account-1",
+        instrument_id="instrument-1",
+        fact_instrument_id=uuid4(),
+        lot_size=row.lot_size,
+        owner_runtime_instance_id=identity.instance_id,
+        owner_generation=3,
+    )
+    wrong_lot_size = source.execution_snapshot(
+        broker_id=broker_id,
+        automation_id=automation_id,
+        account_id="account-1",
+        instrument_id="instrument-1",
+        fact_instrument_id=row.fact_instrument_id,
+        lot_size=11,
+        owner_runtime_instance_id=identity.instance_id,
+        owner_generation=3,
+    )
+
+    assert accepted.observation == "OBSERVED"
+    assert accepted.row == row
+    assert takeover.observation == "UNKNOWN"
+    assert takeover.reason == "OWNER_CHANGED"
+    assert takeover.row is None
+    assert cross_account.observation == "UNKNOWN"
+    assert cross_account.row is None
+    assert wrong_catalog_id.observation == wrong_lot_size.observation == "UNKNOWN"
+    assert wrong_catalog_id.reason == wrong_lot_size.reason == "INVALID_PROJECTION"
+    assert wrong_catalog_id.row is wrong_lot_size.row is None
+
+
+def test_execution_projection_source_ttl_and_future_skew_are_checked():
+    clock = [NOW]
+    ticks = [0.0]
+    source = WorkerVersionStore("TEST", "READ_ONLY", clock=lambda: clock[0], monotonic_clock=lambda: ticks[0])
+    identity = metadata()
+    broker_id = uuid4()
+    automation_id = uuid4()
+    row = WorkerAutomationExecutionDiagnostics(
+        automation_id=automation_id,
+        account_id="account-1",
+        external_instrument_id="instrument-1",
+        fact_instrument_id=uuid4(),
+        lot_size=1,
+        pending_fact_count=0,
+        failed_fact_count=0,
+    )
+
+    def projection(captured_at):
+        return progress().model_copy(
+            update={
+                "execution_diagnostics": WorkerExecutionDiagnostics(
+                    observation="OBSERVED",
+                    reason="OBSERVED",
+                    broker_id=broker_id,
+                    captured_at=captured_at,
+                    rows=(row,),
+                )
+            }
+        )
+
+    source.observe(
+        identity,
+        NOW,
+        projection(NOW),
+        source_runtime_instance_id=identity.instance_id,
+        source_generation=1,
+        source_broker_id=broker_id,
+    )
+    lookup = {
+        "broker_id": broker_id,
+        "automation_id": automation_id,
+        "account_id": "account-1",
+        "instrument_id": "instrument-1",
+        "fact_instrument_id": row.fact_instrument_id,
+        "lot_size": row.lot_size,
+        "owner_runtime_instance_id": identity.instance_id,
+        "owner_generation": 1,
+    }
+    assert source.execution_snapshot(**lookup).observation == "OBSERVED"
+    clock[0] += timedelta(seconds=30)
+    assert source.execution_snapshot(**lookup).reason == "STALE"
+
+    fresh_source, _ = store()
+    fresh = metadata()
+    future_diagnostics = projection(NOW + timedelta(milliseconds=5001))
+    fresh_source.observe(
+        fresh,
+        NOW,
+        future_diagnostics,
+        source_runtime_instance_id=fresh.instance_id,
+        source_generation=1,
+        source_broker_id=broker_id,
+    )
+    assert (
+        fresh_source.execution_snapshot(**{**lookup, "owner_runtime_instance_id": fresh.instance_id}).reason == "STALE"
+    )
+
+
 @pytest.mark.parametrize(("result", "reason"), [("ERROR", "ITERATION_FAILED"), ("OUTBOX_BLOCKED", "OUTBOX_BLOCKED")])
 def test_failed_finished_iteration_is_immediately_degraded(result, reason):
     source, _ = store()
```

## tests/trading_automaton/runtime/__init__.py

```diff
--- baseline/tests/trading_automaton/runtime/__init__.py
+++ tests/trading_automaton/runtime/__init__.py
@@ -0,0 +1 @@
+"""Runtime diagnostics tests."""
```
