"""Dispatch a persisted broker intent without including broker response in SLA."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sentinel_contracts.broker_execution import BrokerOrderState
from sentinel_contracts.business_audit import BusinessAuditStage
from trading_automaton.domain.dtos import DispatchRequest
from trading_automaton.domain.ports import IntentUpdatePort, OrderStageAuditPort


class DispatchBrokerPort(Protocol):
    async def dispatch_limit_order(self, request: DispatchRequest) -> BrokerOrderState: ...

    async def find_by_idempotency_key(self, account_id: str, idempotency_key: str) -> BrokerOrderState | None: ...

    async def inspect_position(self, account_id: str, instrument_id: str) -> dict[str, object] | None: ...

    async def inspect_recent_operations(
        self, account_id: str, instrument_id: str, limit: int
    ) -> tuple[dict[str, object], ...]: ...


class DispatchRepositoryPort(IntentUpdatePort, Protocol):
    pass


class OrderDispatchService:
    def __init__(
        self,
        repository: DispatchRepositoryPort,
        broker: DispatchBrokerPort,
        *,
        now: Callable[[], datetime],
        audit: OrderStageAuditPort | None = None,
    ) -> None:
        self._repository = repository
        self._broker = broker
        self._now = now
        self._audit = audit

    async def dispatch(
        self,
        request: DispatchRequest,
        started: "asyncio.Future[datetime]",
    ) -> BrokerOrderState:
        dispatched_at = self._now()
        if self._market_expired(request):
            return self._cancel_expired(request, started)
        self._repository.update_intent(
            request.idempotency_key,
            state="SUBMITTING",
            occurred_at=dispatched_at,
            dispatch_started_at=dispatched_at,
            process_id=request.process_id,
        )
        if not started.done():
            started.set_result(dispatched_at)
        if self._audit is not None and request.process_id is not None:
            await asyncio.to_thread(
                self._audit.record_order_stage,
                stage=BusinessAuditStage.BROKER_ORDER_DISPATCH_STARTED,
                process_id=request.process_id,
                automation_id=request.automation_id,
                broker_id=request.broker_id,
                account_id=request.account_id,
                instrument_id=request.instrument_id,
                message="Broker order dispatch started",
                side=request.side.value,
                quantity_lots=request.quantity_lots,
                limit_price=str(request.limit_price),
            )
        if self._market_expired(request):
            return self._cancel_expired(request, started)
        try:
            response = await self._broker.dispatch_limit_order(request)
            responded_at = self._now()
            self._repository.update_intent(
                request.idempotency_key,
                state=response.status,
                occurred_at=responded_at,
                broker_order_id=response.broker_order_id,
                broker_responded_at=responded_at,
                process_id=request.process_id,
            )
            if self._audit is not None and request.process_id is not None:
                await asyncio.to_thread(
                    self._audit.record_order_stage,
                    stage=BusinessAuditStage.BROKER_ORDER_DISPATCH_RESULT,
                    process_id=request.process_id,
                    automation_id=request.automation_id,
                    broker_id=request.broker_id,
                    account_id=request.account_id,
                    instrument_id=request.instrument_id,
                    message="Broker order dispatch completed",
                    broker_order_state=response.status,
                    broker_order_id=response.broker_order_id,
                )
            return response
        except Exception as error:
            reconciled, position, operations = await asyncio.gather(
                self._broker.find_by_idempotency_key(request.account_id, request.idempotency_key),
                self._broker.inspect_position(request.account_id, request.instrument_id),
                self._broker.inspect_recent_operations(request.account_id, request.instrument_id, 20),
                return_exceptions=True,
            )
            if isinstance(reconciled, BrokerOrderState):
                responded_at = self._now()
                self._repository.update_intent(
                    request.idempotency_key,
                    state=reconciled.status,
                    occurred_at=responded_at,
                    broker_order_id=reconciled.broker_order_id,
                    broker_responded_at=responded_at,
                    process_id=request.process_id,
                )
                if self._audit is not None and request.process_id is not None:
                    await asyncio.to_thread(
                        self._audit.record_order_stage,
                        stage=BusinessAuditStage.BROKER_ORDER_DISPATCH_RESULT,
                        process_id=request.process_id,
                        automation_id=request.automation_id,
                        broker_id=request.broker_id,
                        account_id=request.account_id,
                        instrument_id=request.instrument_id,
                        message="Broker order reconciled after dispatch failure",
                        broker_order_state=reconciled.status,
                        broker_order_id=reconciled.broker_order_id,
                        exception_type=type(error).__name__,
                        broker_error_code=_error_code(error),
                        broker_error_details=_error_details(error),
                        position_snapshot=None if isinstance(position, Exception) else position,
                        recent_operations=[] if isinstance(operations, BaseException) else list(operations),
                    )
                return reconciled
            self._repository.update_intent(
                request.idempotency_key,
                state="UNCERTAIN",
                occurred_at=self._now(),
                process_id=request.process_id,
            )
            if self._audit is not None and request.process_id is not None:
                await asyncio.to_thread(
                    self._audit.record_order_stage,
                    stage=BusinessAuditStage.TRADING_STEP_FAILED,
                    process_id=request.process_id,
                    automation_id=request.automation_id,
                    broker_id=request.broker_id,
                    account_id=request.account_id,
                    instrument_id=request.instrument_id,
                    message="Broker order dispatch failed",
                    critical=True,
                    broker_order_state="UNCERTAIN",
                    exception_type=type(error).__name__,
                    broker_error_code=_error_code(error),
                    broker_error_details=_error_details(error),
                    reconciliation_error=(type(reconciled).__name__ if isinstance(reconciled, Exception) else None),
                    position_snapshot=None if isinstance(position, Exception) else position,
                    recent_operations=[] if isinstance(operations, BaseException) else list(operations),
                )
            raise

    def _market_expired(self, request: DispatchRequest) -> bool:
        return request.market_valid_until is not None and self._now() > request.market_valid_until

    def _cancel_expired(self, request: DispatchRequest, started: asyncio.Future[datetime]) -> BrokerOrderState:
        occurred_at = self._now()
        self._repository.update_intent(
            request.idempotency_key,
            state="CANCELLED",
            occurred_at=occurred_at,
            process_id=request.process_id,
        )
        if not started.done():
            started.set_result(occurred_at)
        # Tracking finalizes this local cancellation with zero fills and releases reservations.
        return BrokerOrderState(
            broker_order_id="",
            idempotency_key=request.idempotency_key,
            status="CANCELLED",
            requested_lots=request.quantity_lots,
            executed_lots=0,
            requested_amount=request.limit_price * request.quantity_lots * request.lot_size,
            executed_amount=Decimal(),
            estimated_commission=Decimal(),
            executed_commission=Decimal(),
            currency=request.reservation_currency,
        )


def _error_code(error: Exception) -> str | None:
    code = getattr(error, "code", None)
    if not callable(code):
        return None
    value = code()
    return getattr(value, "name", None) or str(value)


def _error_details(error: Exception) -> str:
    details = getattr(error, "details", None)
    value = details() if callable(details) else str(error)
    return str(value)[:1000]
