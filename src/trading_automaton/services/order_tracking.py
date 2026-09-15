"""Own broker response tasks after SDK dispatch has left the hot path."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sentinel_contracts.broker_execution import BrokerOrderState, BrokerPosition
from sentinel_contracts.business_audit import BusinessAuditStage
from trading_automaton.domain.dtos import DispatchRequest, PostCommitCleanupContext
from trading_automaton.domain.ports import IntentUpdatePort, OrderStageAuditPort
from trading_automaton.domain.storage_dtos import (
    AccountCommissionProfile,
    AccountCommissionProfileKey,
    ExecutionFinalization,
    ExecutionFinalizationResult,
)
from trading_automaton.services.active_intent_gate import ActiveIntentGateService

logger = logging.getLogger(__name__)


class TrackingRepositoryPort(IntentUpdatePort, Protocol):
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


class TrackingPortfolioPort(Protocol):
    async def position(self, account_id: str, instrument_id: str) -> BrokerPosition | None: ...

    async def apply_position_event(self, account_id: str, position: BrokerPosition) -> None: ...


class TrackingCashPort(Protocol):
    async def complete(self, intent_id: str, state: str) -> None: ...


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
        audit: OrderStageAuditPort | None = None,
        cash: TrackingCashPort | None = None,
        active_intents: ActiveIntentGateService | None = None,
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
        self._audit = audit
        self._cash = cash
        self._active_intents = active_intents
        self._watchers: set[asyncio.Task[None]] = set()

    def track(
        self,
        intent_id: str,
        dispatch_task: asyncio.Task[BrokerOrderState],
        *,
        request: DispatchRequest | None = None,
    ) -> None:
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
        terminal_response = await self._terminal_response(response, request)
        if terminal_response is None:
            return
        response = terminal_response
        if response.status == "FILLED" and (response.executed_lots <= 0 or response.executed_price <= 0):
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
                "Broker returned FILLED without an executed quantity or price",
                request.automation_id if request is not None else None,
            )
            if self._cash is not None:
                await self._cash.complete(intent_id, "UNCERTAIN")
            await self._audit_stage(
                request,
                BusinessAuditStage.TRADING_STEP_FAILED,
                "Broker returned an inconsistent FILLED state",
                critical=True,
                broker_order_state="UNCERTAIN",
                reason_code="FILLED_EXECUTION_DATA_MISSING",
            )
            return
        timestamp_source = "BROKER_EXECUTION_TIME"
        if response.executed_lots > 0 and response.executed_at is None:
            timestamp_source = "LOCAL_CONFIRMATION_TIME"
        if response.status not in {"FILLED", "REJECTED", "CANCELLED"}:
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
        terminal = {"FILLED", "REJECTED", "CANCELLED"}
        if response.status in terminal or self._broker is None or request is None:
            return response
        for _attempt in range(self._poll_limit):
            await self._sleep(self._poll_seconds)
            response = await self._broker.get_order_state(
                request.account_id,
                response.broker_order_id,
            )
            if response.status in terminal:
                return response
        self._repository.update_intent(
            request.idempotency_key,
            state="UNCERTAIN",
            occurred_at=self._now(),
            process_id=request.process_id,
        )
        if self._cash is not None:
            await self._cash.complete(request.idempotency_key, "UNCERTAIN")
        self._repository.hold_active(
            "Broker order status polling exhausted",
            request.automation_id or None,
        )
        await self._audit_stage(
            request,
            BusinessAuditStage.AUTOMATION_MOVED_TO_HOLD,
            "Automation moved to hold after broker status timeout",
            critical=True,
            broker_order_state="UNCERTAIN",
            reason_code="ORDER_STATUS_POLLING_EXHAUSTED",
        )
        return None

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
