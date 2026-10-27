"""Own broker response tasks after SDK dispatch has left the hot path."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sentinel_contracts.broker_execution import BrokerOrderState, BrokerOrderStatus, BrokerPosition
from sentinel_contracts.broker_order_policy import BROKER_TERMINAL_STATES
from sentinel_contracts.business_audit import BusinessAuditStage
from trading_automaton.domain.dtos import DispatchRequest, PostCommitCleanupContext
from trading_automaton.domain.ports import IntentUpdatePort, OrderStageAuditPort
from trading_automaton.domain.storage_dtos import (
    AccountCommissionProfile,
    AccountCommissionProfileKey,
    ExecutionFinalization,
    ExecutionFinalizationResult,
    LocalIntentRecord,
)
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.order_lifecycle import (
    bounded_broker_call,
    has_admission_origin,
    intent_deadline,
    is_confirmed_nonterminal_status,
)

logger = logging.getLogger(__name__)


class TrackingRepositoryPort(IntentUpdatePort, Protocol):
    def get_active_intent(self, automation_id: str) -> LocalIntentRecord | None: ...

    def hold_active(self, reason: str, automation_id: str | None = None) -> None: ...

    def finalize_execution(self, finalization: ExecutionFinalization) -> ExecutionFinalizationResult: ...


class CommissionObservationPort(Protocol):
    def observe_execution(
        self,
        key: AccountCommissionProfileKey,
        *,
        side: str,
        order_amount: Decimal,
        actual_commission: Decimal,
        now: datetime,
    ) -> AccountCommissionProfile | None: ...


class TrackingBrokerPort(Protocol):
    async def get_order_state(self, account_id: str, broker_order_id: str) -> BrokerOrderState: ...
    async def cancel_order(self, account_id: str, broker_order_id: str) -> None: ...


class TrackingAdmissionPort(Protocol):
    async def validate_cancel(self) -> bool: ...


class TrackingPortfolioPort(Protocol):
    async def position(self, account_id: str, instrument_id: str) -> BrokerPosition | None: ...

    async def apply_position_event(self, account_id: str, position: BrokerPosition) -> None: ...


class TrackingCashPort(Protocol):
    async def complete(self, intent_id: str, state: BrokerOrderStatus) -> None: ...


class PostCommitCleanupError(RuntimeError):
    def __init__(self, context: PostCommitCleanupContext, cause: Exception) -> None:
        super().__init__(str(cause))
        self.context = context
        self.cause = cause


class OrderTrackingService:
    def __init__(
        self,
        repository: TrackingRepositoryPort,
        *,
        now: Callable[[], datetime],
        broker_id: str | None = None,
        commission_profiles: CommissionObservationPort | None = None,
        broker: TrackingBrokerPort | None = None,
        portfolio: TrackingPortfolioPort | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_seconds: float = 0.2,
        poll_limit: int = 50,
        broker_request_timeout_seconds: float = 10.0,
        audit: OrderStageAuditPort | None = None,
        cash: TrackingCashPort | None = None,
        active_intents: ActiveIntentGateService | None = None,
        environment: str = "TEST",
        access_mode: str = "READ_ONLY",
        admission: TrackingAdmissionPort | None = None,
    ) -> None:
        """Track broker outcomes; the repository atomically persists each execution and its ledger changes."""
        self._repository = repository
        self._now = now
        self._broker_id = broker_id
        self._commission_profiles = commission_profiles
        self._broker = broker
        self._portfolio = portfolio
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._poll_limit = poll_limit
        if broker_request_timeout_seconds <= 0:
            raise ValueError("Broker request timeout must be positive")
        self._broker_request_timeout_seconds = broker_request_timeout_seconds
        self._audit = audit
        self._cash = cash
        self._active_intents = active_intents
        if environment not in {"TEST", "PROD"}:
            raise ValueError("Invalid broker environment")
        if access_mode not in {"READ_ONLY", "TRADE"}:
            raise ValueError("Invalid broker access mode")
        self._environment = environment
        self._access_mode = access_mode
        self._admission = admission
        self._watchers: set[asyncio.Task[None]] = set()

    def track(
        self,
        intent_id: str,
        dispatch_task: asyncio.Task[BrokerOrderState],
        *,
        request: DispatchRequest | None = None,
    ) -> None:
        if self._active_intents is not None and not self._active_intents.claim_tracking(intent_id):
            return
        watcher = asyncio.create_task(self._watch(intent_id, dispatch_task, request))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def wait_all(self) -> None:
        if self._watchers:
            await asyncio.gather(*tuple(self._watchers))

    async def _watch(
        self,
        intent_id: str,
        dispatch_task: asyncio.Task[BrokerOrderState],
        request: DispatchRequest | None,
    ) -> None:
        try:
            await self._finish(intent_id, dispatch_task, request)
        except asyncio.CancelledError:
            raise
        except PostCommitCleanupError as error:
            try:
                await self._complete_terminal_local(error.context)
            except asyncio.CancelledError:
                raise
            except Exception as retry_error:
                await self._contain_watcher_failure(
                    error.context.request,
                    "POST_COMMIT_CLEANUP_FAILED",
                    retry_error,
                )
        except Exception as error:
            await self._contain_watcher_failure(
                request,
                "ORDER_TRACKING_WATCHER_FAILED",
                error,
            )
        finally:
            if self._active_intents is not None:
                self._active_intents.release_tracking(intent_id)

    async def _contain_watcher_failure(
        self,
        request: DispatchRequest | None,
        reason_code: str,
        error: Exception,
    ) -> None:
        if request is None or not request.automation_id:
            return
        try:
            self._repository.hold_active("Post-commit terminal cleanup failed", request.automation_id)
        except asyncio.CancelledError:
            raise
        except Exception as hold_error:
            logger.warning(
                "Could not move automation to HOLD after order tracking watcher failure",
                extra={"exception_type": type(hold_error).__name__},
            )
        try:
            await self._audit_stage(
                request,
                BusinessAuditStage.AUTOMATION_MOVED_TO_HOLD,
                "Automation moved to hold after order tracking watcher failure",
                critical=True,
                reason_code=reason_code,
                exception_type=type(error).__name__,
            )
        except asyncio.CancelledError:
            raise
        except Exception as audit_error:
            logger.warning(
                "Could not audit order tracking watcher failure",
                extra={"exception_type": type(audit_error).__name__},
            )

    async def _complete_terminal_local(self, context: PostCommitCleanupContext) -> None:
        if self._cash is not None:
            await self._cash.complete(context.intent_id, context.terminal_state)
        if context.request.automation_id and self._active_intents is not None:
            self._active_intents.clear(context.request.automation_id, context.intent_id)

    async def _finish(
        self,
        intent_id: str,
        dispatch_task: asyncio.Task[BrokerOrderState],
        request: DispatchRequest | None,
    ) -> None:
        try:
            response = await dispatch_task
        except Exception:
            return
        if request is not None and response.idempotency_key != intent_id:
            await self._mark_uncertain(request, "ORDER_REQUEST_ID_MISMATCH")
            return
        terminal_response = await self._terminal_response(response, request)
        if terminal_response is None:
            return
        response = terminal_response
        if (response.status == "FILLED" and response.executed_lots <= 0) or (
            response.status in BROKER_TERMINAL_STATES and response.executed_lots > 0 and response.executed_price <= 0
        ):
            self._repository.update_intent(
                intent_id,
                state="UNCERTAIN",
                occurred_at=self._now(),
                broker_order_id=response.broker_order_id,
                requested_amount=response.requested_amount,
                executed_amount=response.executed_amount,
                estimated_commission=response.estimated_commission,
                executed_commission=response.executed_commission,
                process_id=None if request is None else request.process_id,
            )
            self._repository.hold_active(
                f"Broker returned {response.status} without an executed quantity or price",
                request.automation_id if request is not None else None,
            )
            if self._cash is not None:
                await self._cash.complete(intent_id, "UNCERTAIN")
            await self._audit_stage(
                request,
                BusinessAuditStage.TRADING_STEP_FAILED,
                f"Broker returned an inconsistent {response.status} state",
                critical=True,
                broker_order_state="UNCERTAIN",
                reason_code=f"{response.status}_EXECUTION_DATA_MISSING",
            )
            return
        timestamp_source = "BROKER_EXECUTION_TIME"
        if response.executed_lots > 0 and response.executed_at is None:
            timestamp_source = "LOCAL_CONFIRMATION_TIME"
        if response.status not in BROKER_TERMINAL_STATES:
            self._repository.update_intent(
                intent_id,
                state=response.status,
                occurred_at=self._now(),
                broker_order_id=response.broker_order_id,
                requested_amount=response.requested_amount,
                executed_amount=response.executed_amount,
                estimated_commission=response.estimated_commission,
                executed_commission=response.executed_commission,
                executed_lots=response.executed_lots,
                executed_price=response.executed_price,
                executed_at=response.executed_at,
                process_id=None if request is None else request.process_id,
            )
            return
        if request is None:
            raise ValueError("Terminal broker response requires its dispatch request.")

        terminal_at = self._now()
        result = await asyncio.to_thread(
            self._repository.finalize_execution,
            ExecutionFinalization(
                intent_id=intent_id,
                automation_id=request.automation_id,
                broker_id=request.broker_id or self._broker_id or "",
                account_id=request.account_id,
                instrument_id=request.instrument_id,
                side=request.side.value,
                quantity_lots=request.quantity_lots,
                requested_price=request.limit_price,
                currency=response.currency,
                state=response.status,
                occurred_at=terminal_at,
                broker_order_id=response.broker_order_id,
                requested_amount=response.requested_amount,
                executed_amount=response.executed_amount,
                estimated_commission=response.estimated_commission,
                executed_commission=response.executed_commission,
                executed_lots=response.executed_lots,
                executed_price=response.executed_price,
                executed_at=response.executed_at,
                terminal_at=terminal_at,
                lot_size=request.lot_size,
                process_id=request.process_id,
            ),
        )
        if not result.applied:
            return
        context = PostCommitCleanupContext(intent_id, response.status, request)
        try:
            await self._complete_terminal_local(context)
        except Exception as error:
            raise PostCommitCleanupError(context, error) from error
        snapshot = result.authoritative_position_snapshot
        if snapshot is not None and self._portfolio is not None:
            await self._portfolio.apply_position_event(
                request.account_id,
                BrokerPosition(
                    instrument_id=request.instrument_id,
                    quantity_lots=Decimal(snapshot["quantity_lots"]),
                    average_price=Decimal(snapshot["average_price"]),
                    current_price=response.executed_price,
                    currency=response.currency,
                ),
            )
        await self._audit_stage(
            request,
            BusinessAuditStage.BROKER_ORDER_TERMINAL_STATE,
            "Broker order reached terminal state",
            broker_order_state=response.status,
            broker_order_id=response.broker_order_id,
            executed_lots=response.executed_lots,
            executed_price=str(response.executed_price),
            executed_commission=str(response.executed_commission),
            timestamp_source=timestamp_source,
        )
        if (
            response.executed_lots > 0
            and response.executed_commission >= 0
            and response.requested_amount > 0
            and request is not None
            and self._broker_id is not None
            and self._commission_profiles is not None
        ):
            self._commission_profiles.observe_execution(
                AccountCommissionProfileKey(
                    self._broker_id,
                    request.account_id,
                    request.instrument_type,
                    response.currency,
                ),
                side=request.side.value,
                order_amount=response.requested_amount,
                actual_commission=response.executed_commission,
                now=self._now(),
            )
        if response.executed_lots > 0 and request is not None:
            await self._audit_stage(
                request,
                BusinessAuditStage.POSITION_STATE_UPDATED,
                "Position state updated",
                broker_order_state=response.status,
                executed_lots=response.executed_lots,
                executed_price=str(response.executed_price),
            )
        await self._audit_stage(
            request,
            BusinessAuditStage.TRADING_STEP_COMPLETED,
            "Trading step completed",
            broker_order_state=response.status,
        )

    async def _terminal_response(
        self,
        response: BrokerOrderState,
        request: DispatchRequest | None,
    ) -> BrokerOrderState | None:
        terminal = BROKER_TERMINAL_STATES
        if self._broker is None or request is None:
            return response
        expected_order_id = response.broker_order_id
        intent = None
        deadline = None
        cancel_requested = False
        if self._environment == "PROD":
            intent = await asyncio.to_thread(self._repository.get_active_intent, request.automation_id)
            known_order_id = getattr(intent, "broker_order_id", None)
            if known_order_id and expected_order_id != known_order_id:
                await self._mark_uncertain(request, "ORDER_STATUS_ID_MISMATCH")
                return None
            deadline = intent_deadline(intent, now=self._now()) if intent is not None else None
        if response.status in terminal:
            return response
        if not is_confirmed_nonterminal_status(response.status):
            await self._mark_uncertain(request, "ORDER_STATUS_UNKNOWN")
            return None
        loop = asyncio.get_running_loop()
        confirmation_window = self._poll_seconds * self._poll_limit
        ttl_remaining = max(0.0, (deadline - self._now()).total_seconds()) if deadline is not None else 0.0
        expires_monotonic = loop.time() + ttl_remaining
        confirmation_deadline = expires_monotonic + confirmation_window
        ttl_polls = 0 if self._poll_seconds <= 0 else int(ttl_remaining / self._poll_seconds) + 1
        max_attempts = self._poll_limit + ttl_polls
        for _attempt in range(max_attempts):
            remaining = confirmation_deadline - loop.time()
            if remaining <= 0:
                break
            await self._sleep(min(self._poll_seconds, remaining))
            remaining = confirmation_deadline - loop.time()
            if remaining <= 0:
                break
            try:
                until_expiry = expires_monotonic - loop.time()
                status_timeout = remaining
                if until_expiry > 0:
                    status_timeout = min(status_timeout, until_expiry + self._poll_seconds)
                response = await bounded_broker_call(
                    self._broker.get_order_state(request.account_id, response.broker_order_id),
                    min(self._broker_request_timeout_seconds, status_timeout),
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                break
            except Exception:  # noqa: S112 - bounded same-order polling handles transient status errors.
                continue
            if response.broker_order_id != expected_order_id:
                await self._mark_uncertain(request, "ORDER_STATUS_ID_MISMATCH")
                return None
            if response.status in terminal:
                return response
            if not is_confirmed_nonterminal_status(response.status):
                await self._mark_uncertain(request, "ORDER_STATUS_UNKNOWN")
                return None
            known_order_id = getattr(intent, "broker_order_id", None)
            if known_order_id and response.broker_order_id != known_order_id:
                await self._mark_uncertain(request, "ORDER_STATUS_ID_MISMATCH")
                return None
            if self._environment == "PROD" and deadline is None:
                await self._mark_uncertain(request, "ORDER_TTL_PROOF_MISSING")
                return None
            if self._environment == "PROD" and not cancel_requested and self._now() >= deadline:
                if (
                    self._access_mode != "TRADE"
                    or self._admission is None
                    or not has_admission_origin(intent)
                    or not await self._admission.validate_cancel()
                ):
                    await self._mark_uncertain(request, "ORDER_CANCEL_NOT_AUTHORIZED")
                    return None
                try:
                    remaining = confirmation_deadline - loop.time()
                    if remaining <= 0:
                        break
                    await bounded_broker_call(
                        self._broker.cancel_order(request.account_id, response.broker_order_id),
                        min(self._broker_request_timeout_seconds, remaining / 2),
                    )
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    # Query this same order while any confirmation time remains.
                    pass
                except Exception:  # noqa: S110 - lost ACK is resolved by polling this same order.
                    # Reconcile a lost ACK through same-order status polling below.
                    pass
                # A single watcher sends at most one cancel for this intent.
                cancel_requested = True
        await self._mark_uncertain(request, "ORDER_STATUS_POLLING_EXHAUSTED")
        return None

    async def _mark_uncertain(self, request: DispatchRequest, reason_code: str) -> None:
        self._repository.update_intent(
            request.idempotency_key,
            state="UNCERTAIN",
            occurred_at=self._now(),
            process_id=request.process_id,
        )
        if self._cash is not None:
            await self._cash.complete(request.idempotency_key, "UNCERTAIN")
        hold_reason = (
            "Broker order status polling exhausted"
            if reason_code == "ORDER_STATUS_POLLING_EXHAUSTED"
            else "Broker order outcome remains uncertain"
        )
        self._repository.hold_active(hold_reason, request.automation_id or None)
        await self._audit_stage(
            request,
            BusinessAuditStage.AUTOMATION_MOVED_TO_HOLD,
            "Automation moved to hold after broker order outcome became uncertain",
            critical=True,
            broker_order_state="UNCERTAIN",
            reason_code=reason_code,
        )

    async def _audit_stage(
        self,
        request: DispatchRequest | None,
        stage: BusinessAuditStage,
        message: str,
        *,
        critical: bool = False,
        **data: object,
    ) -> None:
        if self._audit is None or request is None or request.process_id is None:
            return
        await asyncio.to_thread(
            self._audit.record_order_stage,
            stage=stage,
            process_id=request.process_id,
            automation_id=request.automation_id,
            broker_id=request.broker_id or self._broker_id or "",
            account_id=request.account_id,
            instrument_id=request.instrument_id,
            message=message,
            critical=critical,
            **data,
        )
