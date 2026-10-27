"""Reconcile uncertain broker intents without repeating their market effect."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sentinel_contracts.broker_errors import BrokerOperationError
from sentinel_contracts.broker_execution import (
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerRecoveryOperation,
    OrderSide,
)
from sentinel_contracts.broker_order_policy import BROKER_TERMINAL_STATES
from sentinel_contracts.business_audit import BusinessAuditStage
from sentinel_contracts.time import floor_utc_millisecond
from sentinel_contracts.trading_admission import (
    PreRpcAbortProof,
    SubmitAttemptView,
    SubmitAuthorizationRequest,
    submit_request_fingerprint,
)
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.dtos import ReconciliationResult
from trading_automaton.domain.errors import CoreOperationError
from trading_automaton.domain.ports import OrderStageAuditPort
from trading_automaton.domain.storage_dtos import (
    ExecutionFinalization,
    ExecutionFinalizationResult,
    LocalIntentRecord,
    TradeLotRecord,
)
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.order_lifecycle import (
    bounded_broker_call,
    has_admission_origin,
    intent_age_expired,
    intent_deadline,
    is_confirmed_nonterminal_status,
)

_RECOVERY_NAMESPACE = uuid5(NAMESPACE_URL, "urn:moex-sentinel:worker:uncertain-intent-recovery")
_BROKER_ONLY_RECOVERY_STATES = {"FAILED", "ACCEPTED", "PARTIALLY_FILLED"}


class ReconciliationRepositoryPort(Protocol):
    def get_active_intent(self, automation_id: str) -> LocalIntentRecord | None: ...

    def expire_intent_by_age(self, idempotency_key: str, *, expired_at: datetime) -> LocalIntentRecord | None: ...

    def list_policy_expired_unconfirmed(
        self,
        automation_ids: tuple[str, ...],
        *,
        now: datetime,
        limit: int = 100,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[LocalIntentRecord, ...]: ...

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

    async def cancel_order(self, account_id: str, broker_order_id: str) -> None: ...

    async def find_by_idempotency_key(self, account_id: str, idempotency_key: str) -> BrokerOrderState | None: ...

    async def inspect_position(self, account_id: str, instrument_id: str) -> BrokerPosition | None: ...

    async def inspect_recent_operations(
        self, account_id: str, instrument_id: str, limit: int
    ) -> tuple[BrokerRecoveryOperation, ...]: ...


class ReconciliationCashPort(Protocol):
    async def complete(self, intent_id: str, state: BrokerOrderStatus) -> None: ...

    async def release(self, intent_id: str) -> None: ...


class ReconciliationAdmissionPort(Protocol):
    async def validate_cancel(self) -> bool: ...

    async def submit_attempt(
        self,
        idempotency_key: str,
        *,
        origin_runtime_instance_id: UUID | None,
        origin_generation: int | None,
    ) -> SubmitAttemptView: ...


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
        environment: str = "TEST",
        access_mode: str = "READ_ONLY",
        admission: ReconciliationAdmissionPort | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_seconds: float = 0.2,
        poll_limit: int = 50,
        broker_request_timeout_seconds: float = 10.0,
    ) -> None:
        self._repository = repository
        self._broker = broker
        self._now = now
        self._audit = audit
        self._active_intents = active_intents
        self._cash = cash
        if environment not in {"TEST", "PROD"}:
            raise ValueError("Invalid broker environment")
        if access_mode not in {"READ_ONLY", "TRADE"}:
            raise ValueError("Invalid broker access mode")
        if poll_seconds < 0 or poll_limit <= 0 or broker_request_timeout_seconds <= 0:
            raise ValueError("Invalid order status polling budget")
        self._environment = environment
        self._access_mode = access_mode
        self._admission = admission
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._poll_limit = poll_limit
        self._broker_request_timeout_seconds = broker_request_timeout_seconds
        self._attempted: set[str] = set()
        self._retry_after: dict[str, datetime] = {}
        self._age_recovery_cursor: dict[tuple[str, ...], tuple[datetime, str]] = {}

    async def reconcile(self, commands: tuple[AutomationCommand, ...]) -> ReconciliationResult:
        command_by_id = {str(command.automation_id): command for command in commands}
        scope = tuple(sorted(command_by_id))
        cursor = self._age_recovery_cursor.get(scope)
        expired = await asyncio.to_thread(
            self._repository.list_policy_expired_unconfirmed,
            scope,
            now=self._now(),
            limit=100,
            after=cursor,
        )
        if expired:
            last = expired[-1]
            self._age_recovery_cursor[scope] = (last.created_at, last.idempotency_key)
        else:
            self._age_recovery_cursor.pop(scope, None)
        results = await asyncio.gather(
            *(self._reconcile_one(command) for command in commands),
            *(
                self._reconcile_one(command_by_id[intent.automation_id], intent_override=intent)
                for intent in expired
                if intent.automation_id in command_by_id
            ),
        )
        return ReconciliationResult(
            resolved=sum(item == "RESOLVED" for item in results),
            unresolved=sum(item == "UNRESOLVED" for item in results),
        )

    async def _reconcile_one(
        self,
        command: AutomationCommand,
        *,
        intent_override: LocalIntentRecord | None = None,
    ) -> str:
        automation_id = str(command.automation_id)
        intent = intent_override or await asyncio.to_thread(self._repository.get_active_intent, automation_id)
        if intent_override is None and intent is not None and intent_age_expired(intent, now=self._now()):
            expired = await asyncio.to_thread(
                self._repository.expire_intent_by_age,
                intent.idempotency_key,
                expired_at=self._now(),
            )
            if expired is not None:
                intent = expired
                if self._cash is not None:
                    await self._cash.release(intent.idempotency_key)
                if self._active_intents is not None:
                    self._active_intents.clear(automation_id, intent.idempotency_key)
        policy_expired = intent is not None and _has_age_policy_marker(intent, now=self._now())
        if (
            intent is not None
            and self._active_intents is not None
            and self._active_intents.is_tracked(intent.idempotency_key)
            and not policy_expired
        ):
            return "SKIPPED"
        recoverable_states = {"UNCERTAIN", *_BROKER_ONLY_RECOVERY_STATES}
        if self._environment == "PROD":
            recoverable_states.update(
                {
                    "CREATED",
                    "DISPATCH_PENDING",
                    "SUBMITTING",
                    "SUBMITTED",
                    "ACCEPTED",
                    "PARTIALLY_FILLED",
                    "FILLED",
                    "REJECTED",
                    "CANCELLED",
                    "EXPIRED",
                }
            )
        if (
            intent is None
            or (intent.state not in recoverable_states and not policy_expired)
            or intent.idempotency_key in self._attempted
        ):
            return "SKIPPED"
        broker_only = self._environment == "PROD" or intent.state in _BROKER_ONLY_RECOVERY_STATES
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
            if await self._finalize_persisted_pre_rpc_abort(intent, command, process_id):
                return "RESOLVED"
            if intent.state != "FAILED" and await self._cancel_if_proven_not_granted(intent, command, process_id):
                return "RESOLVED"
            if broker_only:
                try:
                    broker_state = await bounded_broker_call(
                        self._lookup_broker_state(command, intent), self._broker_request_timeout_seconds
                    )
                except TimeoutError:
                    broker_state = None
                operations = ()
                if (
                    self._environment == "PROD"
                    and broker_state is not None
                    and intent.broker_order_id
                    and broker_state.broker_order_id != intent.broker_order_id
                ):
                    broker_state = None
                if (
                    self._environment == "PROD"
                    and broker_state is not None
                    and broker_state.status not in BROKER_TERMINAL_STATES
                ):
                    if not is_confirmed_nonterminal_status(broker_state.status):
                        broker_state = None
                    else:
                        deadline = intent_deadline(intent, now=self._now())
                        if deadline is None and not policy_expired:
                            broker_state = None
                        elif deadline is not None and self._now() < deadline:
                            self._retry_after[intent.idempotency_key] = self._now() + timedelta(seconds=10)
                            self._attempted.discard(intent.idempotency_key)
                            return "PENDING"
                        else:
                            broker_state = await self._cancel_expired_order(intent, command, broker_state)
                if broker_state is None or broker_state.status not in BROKER_TERMINAL_STATES:
                    position = None
                elif broker_state.executed_lots > 0:
                    position = await self._broker.inspect_position(
                        command.account_id,
                        command.external_instrument_id,
                    )
                else:
                    position = None
            else:
                broker_state, position, operations = await asyncio.gather(
                    self._lookup_broker_state(command, intent),
                    self._broker.inspect_position(command.account_id, command.external_instrument_id),
                    self._broker.inspect_recent_operations(command.account_id, command.external_instrument_id, 100),
                )
        except (BrokerOperationError, CoreOperationError) as error:
            if error.retryable:
                self._attempted.discard(intent.idempotency_key)
            raise
        reconciled: BrokerOrderState | None = broker_state
        if (
            broker_only
            and self._environment != "PROD"
            and reconciled is not None
            and reconciled.status not in BROKER_TERMINAL_STATES
        ):
            await asyncio.to_thread(
                self._repository.update_intent,
                intent.idempotency_key,
                state=reconciled.status,
                occurred_at=self._now(),
                broker_order_id=reconciled.broker_order_id,
                requested_amount=reconciled.requested_amount,
                executed_amount=reconciled.executed_amount,
                estimated_commission=reconciled.estimated_commission,
                executed_commission=reconciled.executed_commission,
                executed_lots=reconciled.executed_lots,
                executed_price=reconciled.executed_price,
                executed_at=reconciled.executed_at,
            )
            self._retry_after[intent.idempotency_key] = self._now() + timedelta(seconds=10)
            self._attempted.discard(intent.idempotency_key)
            return "PENDING"
        if broker_only and reconciled is not None and reconciled.status not in BROKER_TERMINAL_STATES:
            self._retry_after[intent.idempotency_key] = self._now() + timedelta(seconds=10)
            self._attempted.discard(intent.idempotency_key)
            reconciled = None
        if not broker_only and (reconciled is None or reconciled.status not in BROKER_TERMINAL_STATES):
            reconciled = self._state_from_operations(intent, command, operations)
        elif reconciled is not None and reconciled.status not in BROKER_TERMINAL_STATES:
            reconciled = None
        if reconciled is None or not await self._persist_if_consistent(
            intent, command, position, reconciled, process_id
        ):
            if intent.state not in {"FAILED", "UNCERTAIN"}:
                await asyncio.to_thread(
                    self._repository.update_intent,
                    intent.idempotency_key,
                    state="UNCERTAIN",
                    occurred_at=self._now(),
                    process_id=process_id,
                )
            if not policy_expired:
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

    async def _cancel_if_proven_not_granted(
        self,
        intent: LocalIntentRecord,
        command: AutomationCommand,
        process_id: str,
    ) -> bool:
        if (
            self._environment != "PROD"
            or self._admission is None
            or getattr(intent, "executed_lots", 0)
            or getattr(intent, "broker_order_id", None)
        ):
            return False
        origin = getattr(intent, "strategy_snapshot", {}).get("admission_origin", {})
        try:
            origin_instance_id = UUID(origin["runtime_instance_id"])
            origin_generation = origin["generation"]
            if type(origin_generation) is not int or origin_generation <= 0:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        attempt = await self._admission.submit_attempt(
            intent.idempotency_key,
            origin_runtime_instance_id=origin_instance_id,
            origin_generation=origin_generation,
        )
        if not attempt.proven_not_granted:
            return False
        occurred_at = self._now()
        await asyncio.to_thread(
            self._repository.update_intent,
            intent.idempotency_key,
            state="CANCELLED",
            occurred_at=occurred_at,
            requested_amount=intent.requested_amount,
            terminal_at=occurred_at,
            process_id=process_id,
        )
        if self._cash is not None:
            await self._cash.complete(intent.idempotency_key, "CANCELLED")
        if self._active_intents is not None:
            self._active_intents.clear(str(command.automation_id), intent.idempotency_key)
        await self._audit_stage(
            command,
            process_id,
            BusinessAuditStage.BROKER_ORDER_TERMINAL_STATE,
            "Unsent broker intent confirmed by retired Core generation",
            broker_order_state="CANCELLED",
            reason_code="SUBMIT_NOT_GRANTED",
        )
        await self._audit_stage(
            command,
            process_id,
            BusinessAuditStage.WORKER_RECOVERY_COMPLETED,
            "Unsent broker intent recovery completed",
            broker_order_state="CANCELLED",
            reason_code="SUBMIT_NOT_GRANTED",
        )
        return True

    async def _cancel_expired_order(
        self,
        intent: LocalIntentRecord,
        command: AutomationCommand,
        state: BrokerOrderState,
    ) -> BrokerOrderState | None:
        if state.status in BROKER_TERMINAL_STATES:
            return state
        if not is_confirmed_nonterminal_status(state.status):
            return None
        if (
            self._environment != "PROD"
            or self._access_mode != "TRADE"
            or self._admission is None
            or not state.broker_order_id
            or not has_admission_origin(intent)
        ):
            return None
        if not await self._admission.validate_cancel():
            return None
        loop = asyncio.get_running_loop()
        confirmation_deadline = loop.time() + self._poll_seconds * self._poll_limit
        try:
            remaining = confirmation_deadline - loop.time()
            if remaining <= 0:
                return None
            await bounded_broker_call(
                self._broker.cancel_order(command.account_id, state.broker_order_id),
                min(self._broker_request_timeout_seconds, remaining / 2),
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # Resolve a lost cancel ACK by querying this same order.
            pass
        except Exception:  # noqa: S110 - lost ACK is resolved by polling this same order.
            # A lost cancel ACK is resolved only by querying this same broker order.
            pass
        for _ in range(self._poll_limit):
            remaining = confirmation_deadline - loop.time()
            if remaining <= 0:
                break
            await self._sleep(min(self._poll_seconds, remaining))
            remaining = confirmation_deadline - loop.time()
            if remaining <= 0:
                break
            try:
                confirmed = await bounded_broker_call(
                    self._broker.get_order_state(command.account_id, state.broker_order_id),
                    min(self._broker_request_timeout_seconds, remaining),
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                break
            except Exception:  # noqa: S112 - bounded same-order polling handles transient status errors.
                continue
            if confirmed is None or confirmed.broker_order_id != state.broker_order_id:
                return None
            if confirmed.status in BROKER_TERMINAL_STATES:
                return confirmed
        return None

    async def _finalize_persisted_pre_rpc_abort(
        self,
        intent: LocalIntentRecord,
        command: AutomationCommand,
        process_id: str,
    ) -> bool:
        if self._environment != "PROD":
            return False
        snapshot = getattr(intent, "strategy_snapshot", {})
        proof_data = snapshot.get("_pre_rpc_abort_proof")
        origin = snapshot.get("admission_origin")
        if not isinstance(proof_data, dict) or not isinstance(origin, dict):
            return False
        try:
            proof = PreRpcAbortProof.model_validate(proof_data)
            runtime_id = UUID(origin["runtime_instance_id"])
            generation = origin["generation"]
            revision = origin["revision"]
            fingerprint = origin["configuration_fingerprint"]
            lot_size = origin["lot_size"]
            currency = origin["currency"]
            instrument_uid = origin["instrument_uid"]
            fact_instrument_id = UUID(origin["fact_instrument_id"])
            if (
                type(generation) is not int
                or generation <= 0
                or type(revision) is not int
                or revision <= 0
                or type(lot_size) is not int
                or lot_size <= 0
                or currency != "RUB"
                or not isinstance(instrument_uid, str)
                or not instrument_uid
            ):
                return False
            requested_amount = intent.limit_price * intent.quantity_lots * lot_size
            if (
                intent.requested_amount != requested_amount
                or intent.estimated_commission != 0
                or intent.executed_amount != 0
                or intent.executed_commission != 0
                or intent.executed_price != 0
            ):
                return False
            captured_at = floor_utc_millisecond(self._now())
            authorization = SubmitAuthorizationRequest(
                broker_id=command.broker_id,
                account_id=command.account_id,
                automation_id=command.automation_id,
                idempotency_key=UUID(intent.idempotency_key),
                generation=generation,
                revision=revision,
                configuration_fingerprint=fingerprint,
                instrument_uid=instrument_uid,
                lot_size=lot_size,
                currency=currency,
                side=intent.side,
                quantity_lots=intent.quantity_lots,
                limit_price=intent.limit_price,
                market_valid_until=captured_at,
                submit_deadline=captured_at,
            )
        except (KeyError, TypeError, ValueError):
            return False
        if (
            proof.idempotency_key != authorization.idempotency_key
            or proof.runtime_instance_id != runtime_id
            or proof.generation != generation
            or proof.configuration_fingerprint != fingerprint
            or proof.request_fingerprint != submit_request_fingerprint(authorization)
            or intent.state != "CANCELLED"
            or intent.broker_order_id not in {None, ""}
            or intent.executed_lots != 0
            or proof.reason not in {"PERMIT_EXPIRED_BEFORE_POST", "MARKET_EXPIRED_BEFORE_POST"}
        ):
            return False
        local_state = BrokerOrderState(
            broker_order_id="",
            idempotency_key=intent.idempotency_key,
            status="CANCELLED",
            requested_lots=intent.quantity_lots,
            executed_lots=0,
            requested_amount=intent.requested_amount,
            executed_amount=Decimal(),
            estimated_commission=intent.estimated_commission,
            executed_commission=Decimal(),
            currency=currency,
        )
        source_command = command.model_copy(
            update={
                "lot_size": lot_size,
                "currency": currency,
                "external_instrument_id": instrument_uid,
                "instrument_id": fact_instrument_id,
            }
        )
        if not await self._persist_if_consistent(intent, source_command, None, local_state, process_id):
            return False
        if self._cash is not None:
            await self._cash.complete(intent.idempotency_key, "CANCELLED")
        if self._active_intents is not None:
            self._active_intents.clear(str(command.automation_id), intent.idempotency_key)
        await self._audit_stage(
            command,
            process_id,
            BusinessAuditStage.BROKER_ORDER_TERMINAL_STATE,
            "Persisted pre-RPC proof completed after Worker restart",
            broker_order_state="CANCELLED",
            reason_code="PRE_RPC_ABORT_PROOF_RECOVERED",
        )
        await self._audit_stage(
            command,
            process_id,
            BusinessAuditStage.WORKER_RECOVERY_COMPLETED,
            "Pre-RPC abort recovery completed",
            broker_order_state="CANCELLED",
            reason_code="PRE_RPC_ABORT_PROOF_RECOVERED",
        )
        return True

    async def _lookup_broker_state(
        self,
        command: AutomationCommand,
        intent: LocalIntentRecord,
    ) -> BrokerOrderState | None:
        state = await self._broker.find_by_idempotency_key(command.account_id, intent.idempotency_key)
        if state is not None and state.idempotency_key != intent.idempotency_key:
            return None
        return state

    async def _persist_if_consistent(
        self,
        intent: LocalIntentRecord,
        command: AutomationCommand,
        position: BrokerPosition | None,
        state: BrokerOrderState,
        process_id: str,
    ) -> bool:
        ledger_already_applied = False
        if state.status == "FILLED" and state.executed_lots <= 0:
            return False
        if state.executed_lots > 0:
            if state.executed_price <= 0:
                return False
            broker_lots = 0 if position is None else int(position.quantity_lots)
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
        operations: tuple[BrokerRecoveryOperation, ...],
    ) -> BrokerOrderState | None:
        expected_side = OrderSide(intent.side)
        expected_units = intent.quantity_lots * command.lot_size
        matches = []
        for operation in operations:
            occurred_at = floor_utc_millisecond(operation.occurred_at)
            price = operation.price
            quantity = operation.quantity_units
            if (
                operation.side == expected_side
                and operation.executed
                and occurred_at is not None
                and intent.created_at <= occurred_at <= intent.created_at + self._WINDOW
                and quantity == expected_units
                and _satisfies_limit(intent.side, price, intent.limit_price)
            ):
                matches.append((operation, occurred_at, price))
        if len(matches) != 1:
            return None
        operation, occurred_at, price = matches[0]
        commission = abs(operation.commission)
        executed_amount = price * expected_units
        return BrokerOrderState(
            broker_order_id=operation.operation_id or intent.idempotency_key,
            idempotency_key=intent.idempotency_key,
            status="FILLED",
            requested_lots=intent.quantity_lots,
            executed_lots=intent.quantity_lots,
            requested_amount=intent.limit_price * expected_units,
            executed_amount=executed_amount,
            estimated_commission=intent.estimated_commission,
            executed_commission=commission,
            currency=operation.currency or command.currency,
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


def _satisfies_limit(side: str, price: Decimal, limit_price: Decimal) -> bool:
    if price <= 0:
        return False
    return price >= limit_price if side == "SELL" else price <= limit_price


def _has_age_policy_marker(intent: LocalIntentRecord, *, now: datetime) -> bool:
    snapshot = getattr(intent, "strategy_snapshot", None)
    marker = snapshot.get("_age_policy") if isinstance(snapshot, dict) else None
    if (
        intent.state != "FAILED"
        or intent.terminal_at is not None
        or not isinstance(marker, dict)
        or set(marker) != {"version", "expired_at"}
        or type(marker.get("version")) is not int
        or marker["version"] != 1
        or not isinstance(marker.get("expired_at"), str)
    ):
        return False
    try:
        expired_at = datetime.fromisoformat(marker["expired_at"])
    except ValueError:
        return False
    return (
        expired_at.tzinfo is not None
        and expired_at.utcoffset() == timedelta(0)
        and expired_at.isoformat(timespec="milliseconds") == marker["expired_at"]
        and expired_at <= now
        and intent_age_expired(intent, now=expired_at)
    )
