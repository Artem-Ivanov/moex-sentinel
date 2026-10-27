"""Hydrate durable position state before creating the SLA market snapshot."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from uuid import UUID, uuid4

from sentinel_contracts.analytics import MarketIndicators
from sentinel_contracts.broker_execution import BrokerPosition
from sentinel_contracts.business_audit import BusinessAuditStage
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.dtos import HydratedPositionState, PositionConsistencyResult
from trading_automaton.domain.storage_dtos import IntentHistory, TradeLotRecord, TradingCycleState


class HydrationRepositoryPort(Protocol):
    def intent_history(self, automation_id: str) -> IntentHistory: ...

    def list_open_lots(self, automation_id: str) -> list[TradeLotRecord]: ...

    def get_cycle_state(self, automation_id: str, *, now: datetime) -> TradingCycleState: ...

    def get_position_cycle_id(self, automation_id: str) -> str | None: ...

    def get_active_intent(self, automation_id: str) -> object | None: ...

    def realized_pnl(self, automation_id: str) -> Decimal: ...

    def has_pending_fact_outbox(self, automation_id: str) -> bool: ...


class PortfolioCachePort(Protocol):
    async def position(self, account_id: str, instrument_id: str) -> BrokerPosition | None: ...


class PreparedMetricsPort(Protocol):
    async def get(self, instrument_id: str) -> MarketIndicators | None: ...


class PositionConsistencyPort(Protocol):
    def reconcile(
        self,
        *,
        automation_id: str,
        broker_lots: int,
        average_price: Decimal,
    ) -> PositionConsistencyResult: ...


class CommissionStatusPort(Protocol):
    def automation_statuses(
        self,
        automation_ids: list[str],
        commission_order_ids: list[str] | None = None,
    ) -> object: ...


class PositionAuditPort(Protocol):
    def record_reconciliation(
        self,
        *,
        stage: BusinessAuditStage,
        process_id: str,
        automation_id: str,
        broker_id: str,
        account_id: str,
        instrument_id: str,
        **data: object,
    ) -> None: ...


class PositionStateCacheService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._values: dict[str, HydratedPositionState] = {}

    async def replace(self, values: dict[str, HydratedPositionState]) -> None:
        async with self._lock:
            self._values = dict(values)

    async def get(self, automation_id: str) -> HydratedPositionState | None:
        async with self._lock:
            return self._values.get(automation_id)

    async def update(self, automation_id: str, value: HydratedPositionState) -> None:
        async with self._lock:
            self._values[automation_id] = value


class PositionStateHydrationService:
    def __init__(
        self,
        repository: HydrationRepositoryPort,
        portfolio: PortfolioCachePort,
        cache: PositionStateCacheService,
        *,
        consistency: PositionConsistencyPort | None = None,
        audit: PositionAuditPort | None = None,
        commission_statuses: CommissionStatusPort | None = None,
        id_factory: Callable[[], str] = lambda: str(uuid4()),
        now: Callable[[], datetime],
        prepared_metrics: PreparedMetricsPort,
    ) -> None:
        self._repository = repository
        self._portfolio = portfolio
        self._cache = cache
        self._now = now
        self._prepared_metrics = prepared_metrics
        self._consistency = consistency
        self._audit = audit
        self._commission_statuses = commission_statuses
        self._commission_cache: dict[tuple[str, str], tuple[datetime, dict[str, object]]] = {}
        self._commission_high_water: dict[tuple[str, str], Decimal] = {}
        self._commission_offsets: dict[str, int] = {}
        self._commission_lock = asyncio.Lock()
        self._id_factory = id_factory
        # One committed success per currently monitored automation; restart records it again.
        self._successful_reconciliations: dict[str, tuple[object, ...]] = {}

    async def hydrate(self, commands: tuple[AutomationCommand, ...]) -> None:
        active_ids = {str(command.automation_id) for command in commands}
        self._successful_reconciliations = {
            key: value for key, value in self._successful_reconciliations.items() if key in active_ids
        }
        results = await asyncio.gather(*(self._hydrate_one(command) for command in commands))
        await self._cache.replace(
            {
                str(command.automation_id): state
                for command, state in zip(commands, results, strict=True)
                if state is not None
            }
        )

    async def _hydrate_one(self, command: AutomationCommand) -> HydratedPositionState | None:
        """Read one state and audit reconciliation; return None when it must be skipped, without updating the cache."""
        if await asyncio.to_thread(self._repository.has_pending_fact_outbox, str(command.automation_id)):
            return None
        position, durable = await asyncio.gather(
            self._portfolio.position(command.account_id, command.external_instrument_id),
            asyncio.to_thread(self._load_durable, str(command.automation_id)),
        )
        if position is None:
            return None
        history, lots, cycle, active_intent, realized_pnl = durable
        process_id = self._id_factory()
        reconciled = False
        if self._consistency is not None:
            try:
                broker_lots = int(position.quantity_lots.to_integral_exact())
            except ArithmeticError:
                broker_lots = -1
            automation_id = str(command.automation_id)
            started_at = self._now()
            worker_lots_before = sum(item.remaining_lots for item in lots)
            cycle_id = (
                await asyncio.to_thread(self._repository.get_position_cycle_id, automation_id)
                if self._audit is not None
                else None
            )
            try:
                consistency = await asyncio.to_thread(
                    self._consistency.reconcile,
                    automation_id=automation_id,
                    broker_lots=broker_lots,
                    average_price=position.average_price,
                )
            except Exception:
                self._successful_reconciliations.pop(automation_id, None)
                await self._audit_reconciliation(
                    command,
                    process_id,
                    BusinessAuditStage.POSITION_RECONCILIATION_FAILED,
                    broker_quantity_lots=broker_lots,
                    worker_quantity_lots=worker_lots_before,
                    reason_code="POSITION_RECONCILIATION_ERROR",
                    include_started=True,
                    started_at=started_at,
                    started_worker_quantity_lots=worker_lots_before,
                )
                raise
            if not consistency.consistent:
                self._successful_reconciliations.pop(automation_id, None)
                await self._audit_reconciliation(
                    command,
                    process_id,
                    BusinessAuditStage.POSITION_RECONCILIATION_FAILED,
                    broker_quantity_lots=broker_lots,
                    worker_quantity_lots=worker_lots_before,
                    reason_code=str(consistency.reason_code),
                    include_started=True,
                    started_at=started_at,
                    started_worker_quantity_lots=worker_lots_before,
                )
                return None
            lots = list(consistency.lots)
            reconciled = True
            worker_lots = sum(item.remaining_lots for item in lots)
            fingerprint = (
                str(command.broker_id),
                command.account_id,
                command.external_instrument_id,
                cycle_id,
                position.quantity_lots,
                worker_lots,
                position.average_price,
                "POSITION_CONSISTENT",
            )
            if cycle_id is None or self._successful_reconciliations.get(automation_id) != fingerprint:
                self._successful_reconciliations.pop(automation_id, None)
                await self._audit_reconciliation(
                    command,
                    process_id,
                    BusinessAuditStage.POSITION_RECONCILED,
                    broker_quantity_lots=broker_lots,
                    worker_quantity_lots=worker_lots,
                    reason_code="POSITION_CONSISTENT",
                    include_started=True,
                    started_at=started_at,
                    started_worker_quantity_lots=worker_lots_before,
                )
                if cycle_id is not None:
                    self._successful_reconciliations[automation_id] = fingerprint
        if reconciled:
            lots = await self._overlay_entry_commissions(command, lots)
            if lots is None:
                return None
        indicators = await self._prepared_metrics.get(command.external_instrument_id)
        if indicators is None:
            return None
        return HydratedPositionState(
            position=position,
            history=history,
            lots=tuple(lots),
            cycle=cycle,
            indicators=indicators,
            has_active_intent=active_intent is not None,
            realized_pnl=realized_pnl,
            process_id=process_id,
        )

    async def _overlay_entry_commissions(
        self, command: AutomationCommand, lots: list[TradeLotRecord]
    ) -> list[TradeLotRecord] | None:
        """Overlay only confirmed fees after reconciliation; unknown sources suppress decisions."""
        if self._commission_statuses is None or not lots:
            return lots
        ids = sorted({lot.source_intent_id for lot in lots if lot.source_intent_id})
        try:
            for value in ids:
                UUID(value)
        except ValueError:
            return None
        async with self._commission_lock:
            now = self._now()
            requested = [
                value
                for value in ids
                if (cached := self._commission_cache.get((str(command.automation_id), value))) is None
                or (now - cached[0]).total_seconds() >= 60
            ]
            if requested:
                if len(requested) > 100:
                    offset = self._commission_offsets.get(str(command.automation_id), 0) % len(requested)
                    requested = (requested + requested)[offset : offset + 100]
                    self._commission_offsets[str(command.automation_id)] = (offset + len(requested)) % len(ids)
                try:
                    result = await asyncio.to_thread(
                        self._commission_statuses.automation_statuses,
                        [str(command.automation_id)],
                        requested,
                    )
                except Exception:
                    return None
                entries = getattr(result, "commission_statuses", {})
                if set(entries) != set(requested):
                    return None
                updates: dict[tuple[str, str], tuple[datetime, dict[str, object]]] = {}
                for source_id, entry in entries.items():
                    if (
                        entry.get("automation_id") != str(command.automation_id)
                        or entry.get("user_broker_id") != str(command.user_broker_id)
                        or entry.get("currency") != command.currency.upper()
                        or entry.get("commission_status") not in {"KNOWN", "MISSING", "UNKNOWN"}
                    ):
                        return None
                    cache_key = (str(command.automation_id), source_id)
                    normalized = dict(entry)
                    if entry["commission_status"] == "KNOWN":
                        amount = entry.get("effective_broker_commission")
                        try:
                            valid_amount = (
                                isinstance(amount, Decimal)
                                and amount.is_finite()
                                and Decimal() <= amount < Decimal("1e19")
                                and amount.quantize(Decimal("1e-9")) == amount
                            )
                        except InvalidOperation:
                            valid_amount = False
                        if not valid_amount:
                            return None
                        previous = self._commission_high_water.get(cache_key)
                        if previous is not None and amount < previous:
                            normalized["commission_status"] = "UNKNOWN"
                            normalized["effective_broker_commission"] = None
                        else:
                            self._commission_high_water[cache_key] = amount
                    updates[cache_key] = (now, normalized)
                self._commission_cache.update(updates)
            projected: list[TradeLotRecord] = []
            for lot in lots:
                if not lot.source_intent_id:
                    projected.append(lot)
                    continue
                cached = self._commission_cache.get((str(command.automation_id), lot.source_intent_id))
                if cached is None:
                    return None
                entry = cached[1]
                status = entry["commission_status"]
                if status == "UNKNOWN":
                    return None
                if status == "KNOWN":
                    total = entry["effective_broker_commission"]
                    if not isinstance(total, Decimal):
                        return None
                    amount = total * Decimal(lot.remaining_lots) / Decimal(lot.original_lots)
                    if amount > lot.entry_commission:
                        lot = lot.model_copy(update={"entry_commission": amount})
                projected.append(lot)
            return projected

    async def _audit_reconciliation(
        self,
        command: AutomationCommand,
        process_id: str,
        stage: BusinessAuditStage,
        **data: object,
    ) -> None:
        if self._audit is None:
            return
        await asyncio.to_thread(
            self._audit.record_reconciliation,
            stage=stage,
            process_id=process_id,
            automation_id=str(command.automation_id),
            broker_id=str(command.broker_id),
            account_id=command.account_id,
            instrument_id=command.external_instrument_id,
            **data,
        )

    def _load_durable(
        self,
        automation_id: str,
    ) -> tuple[IntentHistory, list[TradeLotRecord], TradingCycleState, object | None, Decimal]:
        return (
            self._repository.intent_history(automation_id),
            self._repository.list_open_lots(automation_id),
            self._repository.get_cycle_state(automation_id, now=self._now()),
            self._repository.get_active_intent(automation_id),
            self._repository.realized_pnl(automation_id),
        )
