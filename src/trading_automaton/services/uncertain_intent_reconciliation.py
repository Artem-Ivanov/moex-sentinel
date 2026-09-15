"""Reconcile uncertain broker intents without repeating their market effect."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.broker_execution import BrokerOrderState
from sentinel_contracts.business_audit import BusinessAuditStage
from sentinel_contracts.time import floor_utc_millisecond
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.dtos import ReconciliationResult
from trading_automaton.domain.ports import OrderStageAuditPort
from trading_automaton.domain.storage_dtos import (
    ExecutionFinalization,
    ExecutionFinalizationResult,
    LocalIntentRecord,
    TradeLotRecord,
)
from trading_automaton.services.active_intent_gate import ActiveIntentGateService

_RECOVERY_NAMESPACE = uuid5(NAMESPACE_URL, "urn:moex-sentinel:worker:uncertain-intent-recovery")


class ReconciliationRepositoryPort(Protocol):
    def get_active_intent(self, automation_id: str) -> LocalIntentRecord | None: ...

    def list_open_lots(self, automation_id: str) -> list[TradeLotRecord]: ...

    def finalize_execution(
        self,
        finalization: ExecutionFinalization,
        *,
        ledger_already_applied: bool = False,
    ) -> ExecutionFinalizationResult: ...

    def hold_active(self, reason: str, automation_id: str | None = None) -> None: ...


class ReconciliationBrokerPort(Protocol):
    async def get_order_state(self, account_id: str, broker_order_id: str) -> BrokerOrderState | None: ...

    async def find_by_idempotency_key(self, account_id: str, idempotency_key: str) -> BrokerOrderState | None: ...

    async def inspect_position(self, account_id: str, instrument_id: str) -> dict[str, object] | None: ...

    async def inspect_recent_operations(
        self, account_id: str, instrument_id: str, limit: int
    ) -> tuple[dict[str, object], ...]: ...


class ReconciliationCashPort(Protocol):
    async def complete(self, intent_id: str, state: str) -> None: ...


class UncertainIntentReconciliationService:
    _WINDOW = timedelta(minutes=2)

    def __init__(
        self,
        repository: ReconciliationRepositoryPort,
        broker: ReconciliationBrokerPort,
        *,
        now: Callable[[], datetime],
        audit: OrderStageAuditPort | None = None,
        active_intents: ActiveIntentGateService | None = None,
        cash: ReconciliationCashPort | None = None,
    ) -> None:
        self._repository = repository
        self._broker = broker
        self._now = now
        self._audit = audit
        self._active_intents = active_intents
        self._cash = cash
        self._attempted: set[str] = set()
        self._retry_after: dict[str, datetime] = {}

    async def reconcile(self, commands: tuple[AutomationCommand, ...]) -> ReconciliationResult:
        results = await asyncio.gather(*(self._reconcile_one(command) for command in commands))
        return ReconciliationResult(
            resolved=sum(item == "RESOLVED" for item in results),
            unresolved=sum(item == "UNRESOLVED" for item in results),
        )

    async def _reconcile_one(self, command: AutomationCommand) -> str:
        automation_id = str(command.automation_id)
        intent = await asyncio.to_thread(self._repository.get_active_intent, automation_id)
        if intent is None or intent.state != "UNCERTAIN" or intent.idempotency_key in self._attempted:
            return "SKIPPED"
        retry_after = self._retry_after.get(intent.idempotency_key)
        if retry_after is not None and self._now() < retry_after:
            return "SKIPPED"
        # Keep correlation stable across retries and restarts without storing a new column.
        process_id = str(uuid5(_RECOVERY_NAMESPACE, f"{command.broker_id}:{automation_id}:{intent.idempotency_key}"))
        self._attempted.add(intent.idempotency_key)
        try:
            await self._audit_stage(
                command,
                process_id,
                BusinessAuditStage.WORKER_RECOVERY_STARTED,
                "Uncertain broker intent reconciliation started",
                broker_order_state="UNCERTAIN",
            )
        except Exception:
            self._attempted.discard(intent.idempotency_key)
            raise
        try:
            broker_state, position, operations = await asyncio.gather(
                self._lookup_broker_state(command, intent),
                self._broker.inspect_position(command.account_id, command.external_instrument_id),
                self._broker.inspect_recent_operations(command.account_id, command.external_instrument_id, 100),
            )
        except TInvestAdapterError as error:
            if error.retryable:
                self._attempted.discard(intent.idempotency_key)
            raise
        reconciled: BrokerOrderState | None = broker_state
        if reconciled is None or reconciled.status not in {"FILLED", "REJECTED", "CANCELLED"}:
            reconciled = self._state_from_operations(intent, command, operations)
        if reconciled is None or not await self._persist_if_consistent(
            intent, command, position, reconciled, process_id
        ):
            await asyncio.to_thread(
                self._repository.hold_active,
                "Uncertain broker intent could not be reconciled unambiguously",
                automation_id,
            )
            await self._audit_stage(
                command,
                process_id,
                BusinessAuditStage.AUTOMATION_MOVED_TO_HOLD,
                "Uncertain broker intent remains unresolved",
                critical=True,
                broker_order_state="UNCERTAIN",
                reason_code="UNCERTAIN_RECONCILIATION_AMBIGUOUS",
            )
            self._retry_after[intent.idempotency_key] = self._now() + timedelta(seconds=30)
            self._attempted.discard(intent.idempotency_key)
            return "UNRESOLVED"
        await self._audit_stage(
            command,
            process_id,
            BusinessAuditStage.BROKER_ORDER_TERMINAL_STATE,
            "Uncertain broker intent reconciled",
            broker_order_state=reconciled.status,
            broker_order_id=reconciled.broker_order_id,
            executed_lots=reconciled.executed_lots,
            executed_price=str(reconciled.executed_price),
            reason_code="UNCERTAIN_RECONCILED",
        )
        if self._cash is not None:
            await self._cash.complete(intent.idempotency_key, reconciled.status)
        if self._active_intents is not None:
            self._active_intents.clear(automation_id, intent.idempotency_key)
        await self._audit_stage(
            command,
            process_id,
            BusinessAuditStage.WORKER_RECOVERY_COMPLETED,
            "Uncertain broker intent recovery completed",
            broker_order_state=reconciled.status,
            reason_code="UNCERTAIN_RECONCILED",
        )
        return "RESOLVED"

    async def _lookup_broker_state(
        self,
        command: AutomationCommand,
        intent: LocalIntentRecord,
    ) -> BrokerOrderState | None:
        if intent.broker_order_id is not None:
            try:
                state = await self._broker.get_order_state(command.account_id, intent.broker_order_id)
            except Exception:
                state = None
            if state is not None:
                return state
        return await self._broker.find_by_idempotency_key(command.account_id, intent.idempotency_key)

    async def _persist_if_consistent(
        self,
        intent: LocalIntentRecord,
        command: AutomationCommand,
        position: dict[str, object] | None,
        state: BrokerOrderState,
        process_id: str,
    ) -> bool:
        ledger_already_applied = False
        if state.status == "FILLED":
            if state.executed_lots <= 0 or state.executed_price <= 0:
                return False
            broker_lots = 0 if position is None else int(Decimal(str(position.get("quantity_lots", "0"))))
            open_lots = await asyncio.to_thread(self._repository.list_open_lots, str(command.automation_id))
            ledger_lots = sum(item.remaining_lots for item in open_lots)
            if ledger_lots == broker_lots:
                ledger_already_applied = True
            elif not (
                (intent.side == "SELL" and ledger_lots - state.executed_lots == broker_lots)
                or (intent.side == "BUY" and ledger_lots + state.executed_lots == broker_lots)
            ):
                return False
        occurred_at = self._now()
        await asyncio.to_thread(
            self._repository.finalize_execution,
            ExecutionFinalization(
                intent_id=intent.idempotency_key,
                automation_id=str(command.automation_id),
                broker_id=str(command.broker_id),
                account_id=command.account_id,
                instrument_id=command.external_instrument_id,
                side=intent.side,
                quantity_lots=intent.quantity_lots,
                requested_price=intent.limit_price,
                currency=state.currency,
                state=state.status,
                occurred_at=occurred_at,
                broker_order_id=state.broker_order_id,
                requested_amount=state.requested_amount,
                executed_amount=state.executed_amount,
                estimated_commission=state.estimated_commission,
                executed_commission=state.executed_commission,
                executed_lots=state.executed_lots,
                executed_price=state.executed_price,
                executed_at=state.executed_at,
                terminal_at=occurred_at,
                lot_size=command.lot_size,
                process_id=process_id,
            ),
            ledger_already_applied=ledger_already_applied,
        )
        return True

    def _state_from_operations(
        self,
        intent: LocalIntentRecord,
        command: AutomationCommand,
        operations: tuple[dict[str, object], ...],
    ) -> BrokerOrderState | None:
        expected_type = f"OPERATION_TYPE_{intent.side}"
        expected_units = intent.quantity_lots * command.lot_size
        matches = []
        for operation in operations:
            occurred_at = _datetime(operation.get("occurred_at"))
            price = _decimal(operation.get("price"))
            quantity = _decimal(operation.get("quantity_done"))
            if (
                operation.get("operation_type") == expected_type
                and operation.get("state") == "OPERATION_STATE_EXECUTED"
                and occurred_at is not None
                and intent.created_at <= occurred_at <= intent.created_at + self._WINDOW
                and quantity == expected_units
                and _satisfies_limit(intent.side, price, intent.limit_price)
            ):
                matches.append((operation, occurred_at, price))
        if len(matches) != 1:
            return None
        operation, occurred_at, price = matches[0]
        commission = abs(_decimal(operation.get("commission")))
        executed_amount = price * expected_units
        return BrokerOrderState(
            str(operation.get("operation_id") or intent.idempotency_key),
            intent.idempotency_key,
            "FILLED",
            intent.quantity_lots,
            intent.quantity_lots,
            intent.limit_price * expected_units,
            executed_amount,
            intent.estimated_commission,
            commission,
            str(operation.get("currency") or command.currency),
            executed_price=price,
            executed_at=occurred_at,
        )

    async def _audit_stage(
        self,
        command: AutomationCommand,
        process_id: str,
        stage: BusinessAuditStage,
        message: str,
        *,
        critical: bool = False,
        **data: object,
    ) -> None:
        if self._audit is None:
            return
        await asyncio.to_thread(
            self._audit.record_order_stage,
            stage=stage,
            process_id=process_id,
            automation_id=str(command.automation_id),
            broker_id=str(command.broker_id),
            account_id=command.account_id,
            instrument_id=command.external_instrument_id,
            message=message,
            critical=critical,
            **data,
        )


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return floor_utc_millisecond(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal()


def _satisfies_limit(side: str, price: Decimal, limit_price: Decimal) -> bool:
    if price <= 0:
        return False
    return price >= limit_price if side == "SELL" else price <= limit_price
