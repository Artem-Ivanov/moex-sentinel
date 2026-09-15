"""Hydrate durable position state before creating the SLA market snapshot."""

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from sentinel_contracts.analytics import MarketIndicators
from sentinel_contracts.broker_execution import BrokerPosition
from sentinel_contracts.business_audit import BusinessAuditStage
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.config import StrategySettings
from trading_automaton.domain.dtos import AdaptiveThresholds, HydratedPositionState, PositionConsistencyResult
from trading_automaton.domain.storage_dtos import IntentHistory, TradeLotRecord, TradingCycleState
from trading_automaton.services.market_indicators import MarketIndicatorsService


class HydrationRepositoryPort(Protocol):
    def intent_history(self, automation_id: str) -> IntentHistory: ...

    def list_open_lots(self, automation_id: str) -> list[TradeLotRecord]: ...

    def get_cycle_state(self, automation_id: str, *, now: datetime) -> TradingCycleState: ...

    def get_active_intent(self, automation_id: str) -> object | None: ...

    def realized_pnl(self, automation_id: str) -> Decimal: ...

    def has_pending_fact_outbox(self, automation_id: str) -> bool: ...


class PortfolioCachePort(Protocol):
    async def position(self, account_id: str, instrument_id: str) -> BrokerPosition | None: ...


class CandleCachePort(Protocol):
    async def completed(self, instrument_id: str) -> Sequence[object]: ...


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
        candles: CandleCachePort | None,
        cache: PositionStateCacheService,
        *,
        consistency: PositionConsistencyPort | None = None,
        audit: PositionAuditPort | None = None,
        settings: StrategySettings | None = None,
        id_factory: Callable[[], str] = lambda: str(uuid4()),
        now: Callable[[], datetime],
        indicators: MarketIndicatorsService | None = None,
        prepared_metrics: PreparedMetricsPort | None = None,
    ) -> None:
        self._repository = repository
        self._portfolio = portfolio
        self._candles = candles
        self._cache = cache
        self._now = now
        self._indicators = indicators or MarketIndicatorsService()
        self._prepared_metrics = prepared_metrics
        self._settings = settings or StrategySettings()
        self._consistency = consistency
        self._audit = audit
        self._id_factory = id_factory

    async def hydrate(self, commands: tuple[AutomationCommand, ...]) -> None:
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
        if self._consistency is not None:
            try:
                broker_lots = int(position.quantity_lots.to_integral_exact())
            except ArithmeticError:
                broker_lots = -1
            await self._audit_reconciliation(
                command,
                process_id,
                BusinessAuditStage.POSITION_RECONCILIATION_STARTED,
                broker_quantity_lots=broker_lots,
                worker_quantity_lots=sum(item.remaining_lots for item in lots),
                reason_code="POSITION_RECONCILIATION_STARTED",
            )
            consistency = await asyncio.to_thread(
                self._consistency.reconcile,
                automation_id=str(command.automation_id),
                broker_lots=broker_lots,
                average_price=position.average_price,
            )
            if not consistency.consistent:
                await self._audit_reconciliation(
                    command,
                    process_id,
                    BusinessAuditStage.POSITION_RECONCILIATION_FAILED,
                    broker_quantity_lots=broker_lots,
                    worker_quantity_lots=sum(item.remaining_lots for item in lots),
                    reason_code=str(consistency.reason_code),
                )
                return None
            lots = list(consistency.lots)
            await self._audit_reconciliation(
                command,
                process_id,
                BusinessAuditStage.POSITION_RECONCILED,
                broker_quantity_lots=broker_lots,
                worker_quantity_lots=sum(item.remaining_lots for item in lots),
                reason_code="POSITION_CONSISTENT",
            )
        fallback = AdaptiveThresholds(
            self._settings.averaging_step_percent,
            self._settings.partial_take_profit_percent,
            "STRATEGY",
        )
        if self._prepared_metrics is not None:
            indicators = await self._prepared_metrics.get(command.external_instrument_id)
            if indicators is None:
                return None
        else:
            candles = () if self._candles is None else await self._candles.completed(command.external_instrument_id)
            indicators = self._indicators.calculate(candles, fallback)  # type: ignore[arg-type]
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
