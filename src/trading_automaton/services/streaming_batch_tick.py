"""Assemble one prepared market snapshot into an atomic batch + dispatch call."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast
from uuid import uuid4

from sentinel_contracts.broker_execution import OrderBookSnapshot
from sentinel_contracts.streaming_market import InstrumentMarketState, MarketBatchSnapshot
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.dtos import DispatchRequest, HydratedPositionState, PositionWorkItem
from trading_automaton.domain.storage_dtos import DecisionBatchItem
from trading_automaton.services.batch_runtime import PostCommitBatchError
from trading_automaton.services.decision_materialization import (
    DecisionMaterializerPort,
    DecisionMaterializerService,
)
from trading_automaton.services.order_book_validation import OrderBookValidationService
from trading_automaton.services.position_batch_scheduler import PositionBatchSchedulerService

LOGGER = logging.getLogger(__name__)


class PositionStatePort(Protocol):
    async def get(self, automation_id: str) -> HydratedPositionState | None: ...

    async def update(self, automation_id: str, value: HydratedPositionState) -> None: ...


class CycleTransitionPort(Protocol):
    def apply(
        self,
        command: AutomationCommand,
        state: HydratedPositionState,
        market: InstrumentMarketState,
        *,
        snapshot_at: datetime | None = None,
    ) -> HydratedPositionState: ...


class BatchRuntimePort(Protocol):
    async def run_batch(
        self,
        items: tuple[DecisionBatchItem, ...],
        requests: tuple[DispatchRequest, ...],
        *,
        snapshot_at: datetime,
    ) -> object: ...


class BusinessAuditPort(Protocol):
    def record_decision_process(
        self,
        *,
        process_id: str,
        automation_id: str,
        broker_id: str,
        account_id: str,
        instrument_id: str,
        **data: object,
    ) -> None: ...


class CashReservationPort(Protocol):
    async def available(self, account_id: str, currency: str) -> Decimal: ...

    async def reserved(self, account_id: str, currency: str) -> Decimal: ...


class StreamingBatchTickService:
    def __init__(
        self,
        scheduler: PositionBatchSchedulerService,
        states: PositionStatePort,
        commissions: object,
        batch: BatchRuntimePort,
        *,
        cash: CashReservationPort | None = None,
        cycles: CycleTransitionPort | None = None,
        now: Callable[[], datetime],
        audit: BusinessAuditPort | None = None,
        id_factory: Callable[[], str] = lambda: str(uuid4()),
        materializer: DecisionMaterializerPort | None = None,
        max_order_book_age: timedelta = timedelta(seconds=2),
    ) -> None:
        self._scheduler = scheduler
        self._states = states
        self._batch = batch
        self._cash = cash
        self._cycles = cycles
        self._audit = audit
        self._materializer = materializer or DecisionMaterializerService(id_factory=id_factory)
        self._order_books = OrderBookValidationService()
        self._max_order_book_age = max_order_book_age
        self._now = now
        # Serializing tick handling makes parallel cash budgeting deterministic.
        self._tick_lock = asyncio.Lock()

    async def run_tick(
        self,
        commands: tuple[AutomationCommand, ...],
        snapshot: MarketBatchSnapshot,
    ) -> object:
        async with self._tick_lock:
            if self._expired(snapshot):
                return None
            return await self._run_tick(commands, snapshot)

    async def _run_tick(
        self,
        commands: tuple[AutomationCommand, ...],
        snapshot: MarketBatchSnapshot,
    ) -> object:
        loaded_states = await asyncio.gather(*(self._states.get(str(item.automation_id)) for item in commands))
        if self._expired(snapshot):
            return None
        states = list(loaded_states)
        loaded_by_id = {
            str(command.automation_id): state
            for command, state in zip(commands, loaded_states, strict=True)
            if state is not None
        }
        cycle_before = {
            str(command.automation_id): state.cycle.pending_low if state is not None else None
            for command, state in zip(commands, states, strict=True)
        }
        if self._cycles is not None:
            self._apply_cycle_transitions(commands, snapshot, states)

        work = self._build_work_items(commands, states, snapshot.created_at)
        prepared = await self._scheduler.prepare(work, snapshot)

        items: list[DecisionBatchItem] = []
        requests: list[DispatchRequest] = []
        audit_values: list[dict[str, object]] = []
        state_by_id = {
            str(command.automation_id): state
            for command, state in zip(commands, states, strict=True)
            if state is not None
        }
        work_by_id = {str(item.command.automation_id): item for item in work}
        pending_cash: dict[tuple[str, str], Decimal] = {}

        for result in prepared:
            market = snapshot.instruments.get(result.command.external_instrument_id)
            if market is None or market.order_book is None:
                continue
            book = market.order_book
            if (
                not book.bids
                or not book.asks
                or any(not level.price.is_finite() or level.price <= 0 for level in (book.best_bid, book.best_ask))
            ):
                LOGGER.warning(
                    "Market snapshot rejected before valuation",
                    extra={
                        "reason_code": result.decision.reason_code,
                        "automation_id": str(result.command.automation_id),
                        "data": {"snapshot_id": snapshot.snapshot_id},
                    },
                )
                continue
            if not self._valid_market(market, snapshot.created_at) and result.decision.kind not in {
                DecisionKind.WAIT,
                DecisionKind.NO_ACTION,
            }:
                continue

            work_item = work_by_id[str(result.command.automation_id)]
            materialized = await self._materializer.materialize(
                result,
                work_item,
                market,
                cycle_before.get(str(result.command.automation_id)),
                cash=self._cash,
                pending_cash=pending_cash,
                snapshot_at=snapshot.created_at,
            )
            if materialized is None:
                continue

            item = materialized.item
            loaded = loaded_by_id.get(item.automation_id)
            if item.cycle_state is not None and loaded is not None:
                item = item.model_copy(
                    update={
                        "cycle_state": {**item.cycle_state, "expected_state": loaded.cycle.model_dump()},
                    }
                )
            items.append(item)
            if materialized.request is not None:
                requests.append(materialized.request.model_copy(update={"market_valid_until": snapshot.expires_at}))
            if self._audit is not None:
                audit_values.append(materialized.audit_values)

        if self._expired(snapshot):
            return None
        try:
            batch_result = await self._batch.run_batch(tuple(items), tuple(requests), snapshot_at=snapshot.created_at)
        except PostCommitBatchError as error:
            await self._publish_committed_states(items, state_by_id, loaded_by_id, error.persisted)
            raise

        persisted = getattr(batch_result, "persisted", None)
        await self._publish_committed_states(items, state_by_id, loaded_by_id, persisted)

        if self._audit is not None:
            persisted_decisions = getattr(persisted, "decisions", ())
            actual_by_automation = {item.automation_id: item for item in persisted_decisions}
            for values in audit_values:
                actual = actual_by_automation.get(values["automation_id"])
                if actual is not None:
                    values["decision"] = actual.decision
                    values["reason_code"] = actual.reason_code
                recorder = cast(Callable[..., None], self._audit.record_decision_process)
                await asyncio.to_thread(recorder, **values)

        return batch_result

    def _expired(self, snapshot: MarketBatchSnapshot) -> bool:
        if snapshot.expires_at is None:
            return False
        now = self._now()
        return now < snapshot.created_at or now > snapshot.expires_at

    def _apply_cycle_transitions(
        self,
        commands: tuple[AutomationCommand, ...],
        snapshot: MarketBatchSnapshot,
        states: list[HydratedPositionState | None],
    ) -> None:
        cycles = self._cycles
        if cycles is None:
            return
        for index, (command, state) in enumerate(zip(commands, states, strict=True)):
            if state is None:
                continue
            market = snapshot.instruments.get(command.external_instrument_id)
            if market is None or not self._valid_market(market, snapshot.created_at):
                continue
            states[index] = cycles.apply(command, state, market, snapshot_at=snapshot.created_at)

    def _valid_market(self, market: InstrumentMarketState | None, snapshot_at: datetime) -> bool:
        if market is None or market.order_book is None or not market.order_book.is_consistent:
            return False
        book = market.order_book
        return self._order_books.validate(
            OrderBookSnapshot(book.bids, book.asks, book.captured_at),
            now=snapshot_at,
            max_age=self._max_order_book_age,
        ).valid

    def _build_work_items(
        self,
        commands: tuple[AutomationCommand, ...],
        states: list[HydratedPositionState | None],
        snapshot_at: datetime,
    ) -> tuple[PositionWorkItem, ...]:
        return tuple(
            PositionWorkItem(
                command,
                state.has_active_intent if state is not None else False,
                state=state,
                snapshot_at=snapshot_at,
            )
            for command, state in zip(commands, states, strict=True)
        )

    async def _publish_committed_states(
        self,
        items: list[DecisionBatchItem],
        state_by_id: dict[str, HydratedPositionState],
        loaded_by_id: dict[str, HydratedPositionState],
        persisted: object,
    ) -> None:
        active_automation_ids = {item.automation_id for item in getattr(persisted, "intents", ())}
        for automation_id in {item.automation_id for item in items}:
            state = state_by_id.get(automation_id)
            if state is None:
                continue
            committed_state = (
                state.model_copy(update={"has_active_intent": True})
                if automation_id in active_automation_ids
                else state
            )
            if committed_state != loaded_by_id.get(automation_id):
                await self._states.update(automation_id, committed_state)
