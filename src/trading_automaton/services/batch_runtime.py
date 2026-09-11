"""Persist one decision batch and start all broker dispatches in parallel."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sentinel_contracts.broker_execution import BrokerOrderState
from trading_automaton.domain.dtos import BatchTickResult, CommittedCashReservation, DispatchRequest
from trading_automaton.domain.errors import DurableDecisionPersistenceError
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.trading_sla import TradingSlaService
from trading_automaton.storage.repository import (
    BatchPersistResult,
    DecisionBatchItem,
)


class BatchRepositoryPort(Protocol):
    def save_decision_batch(
        self,
        items: tuple[DecisionBatchItem, ...],
        *,
        occurred_at: datetime,
    ) -> BatchPersistResult: ...


class BatchDispatcherPort(Protocol):
    async def dispatch(
        self,
        request: DispatchRequest,
        started: asyncio.Future[datetime],
    ) -> BrokerOrderState: ...


class BatchTrackingPort(Protocol):
    def track(
        self,
        intent_id: str,
        task: asyncio.Task[BrokerOrderState],
        *,
        request: DispatchRequest | None = None,
    ) -> None: ...


class BatchCashPort(Protocol):
    async def reserve_committed_batch(self, values: tuple[CommittedCashReservation, ...]) -> None: ...


class PostCommitBatchError(RuntimeError):
    """A committed batch failed during an in-memory or dispatch side effect."""

    def __init__(self, persisted: BatchPersistResult, cause: Exception) -> None:
        super().__init__(str(cause))
        self.persisted = persisted
        self.cause = cause


class BatchTradingRuntimeService:
    def __init__(
        self,
        repository: BatchRepositoryPort,
        dispatcher: BatchDispatcherPort,
        tracking: BatchTrackingPort,
        *,
        now: Callable[[], datetime],
        sla: TradingSlaService | None = None,
        cash: BatchCashPort | None = None,
        active_intents: ActiveIntentGateService | None = None,
    ) -> None:
        self._repository = repository
        self._dispatcher = dispatcher
        self._tracking = tracking
        self._now = now
        self._sla = sla or TradingSlaService()
        self._cash = cash
        self._active_intents = active_intents

    async def run_batch(
        self,
        items: tuple[DecisionBatchItem, ...],
        requests: tuple[DispatchRequest, ...],
        *,
        snapshot_at: datetime,
    ) -> BatchTickResult:
        request_by_id = self._validate_mapping(items, requests)
        try:
            persisted = self._repository.save_decision_batch(
                items,
                occurred_at=self._now(),
            )
        except Exception as error:
            raise DurableDecisionPersistenceError("Durable decision batch was not stored") from error
        if self._active_intents is not None:
            for intent in persisted.intents:
                self._active_intents.activate(intent.automation_id, intent.idempotency_key)
        try:
            if self._cash is not None:
                reservations = tuple(
                    CommittedCashReservation(
                        account_id=request_by_id[intent.idempotency_key].account_id,
                        currency=request_by_id[intent.idempotency_key].reservation_currency,
                        intent_id=intent.idempotency_key,
                        amount=request_by_id[intent.idempotency_key].required_cash,
                    )
                    for intent in persisted.intents
                    if request_by_id[intent.idempotency_key].required_cash > 0
                )
                await self._cash.reserve_committed_batch(reservations)
            dispatch_starts: list[tuple[asyncio.Future[datetime], asyncio.Task[BrokerOrderState]]] = []
            for intent in persisted.intents:
                request = request_by_id[intent.idempotency_key]
                started = asyncio.get_running_loop().create_future()
                task = asyncio.create_task(self._dispatcher.dispatch(request, started))
                self._tracking.track(intent.idempotency_key, task, request=request)
                dispatch_starts.append((started, task))
            dispatched = await asyncio.gather(
                *(self._wait_for_dispatch_start(started, task) for started, task in dispatch_starts)
            )
        except Exception as error:
            raise PostCommitBatchError(persisted, error) from error
        return BatchTickResult(
            persisted=persisted,
            sla=tuple(self._sla.classify(snapshot_at, item) for item in dispatched),
        )

    @staticmethod
    def _validate_mapping(
        items: tuple[DecisionBatchItem, ...],
        requests: tuple[DispatchRequest, ...],
    ) -> dict[str, DispatchRequest]:
        intent_items = tuple(item for item in items if item.intent is not None)
        intent_ids = tuple(item.intent.idempotency_key for item in intent_items if item.intent is not None)
        request_ids = tuple(request.idempotency_key for request in requests)
        if len(intent_ids) != len(set(intent_ids)) or len(request_ids) != len(set(request_ids)):
            raise ValueError("intent request mapping contains duplicate identifiers")
        if set(intent_ids) != set(request_ids):
            raise ValueError("intent request mapping is not bijective")
        request_by_id = {request.idempotency_key: request for request in requests}
        for item in intent_items:
            intent = item.intent
            if intent is None:
                continue
            request = request_by_id[intent.idempotency_key]
            expected_cash = (
                intent.limit_price * item.lot_size * intent.quantity_lots + item.estimated_commission
                if intent.side == "BUY"
                else Decimal()
            )
            if (
                request.automation_id != item.automation_id
                or request.broker_id != item.broker_id
                or request.account_id != item.account_id
                or request.reservation_currency.upper() != item.currency.upper()
                or request.instrument_id != item.instrument_id
                or request.instrument_type != item.instrument_type
                or request.lot_size != item.lot_size
                or request.process_id != item.process_id
                or request.side.value != intent.side
                or request.quantity_lots != intent.quantity_lots
                or request.limit_price != intent.limit_price
                or item.decision != intent.kind
                or request.required_cash != expected_cash
            ):
                raise ValueError("intent request mapping fields do not match")
        return request_by_id

    @staticmethod
    async def _wait_for_dispatch_start(
        started: asyncio.Future[datetime],
        task: asyncio.Task[BrokerOrderState],
    ) -> datetime:
        done, _pending = await asyncio.wait((started, task), return_when=asyncio.FIRST_COMPLETED)
        if started in done:
            return started.result()
        task.result()
        raise RuntimeError("Broker dispatch completed without marking its start.")
