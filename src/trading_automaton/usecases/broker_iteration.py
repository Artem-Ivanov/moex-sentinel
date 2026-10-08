"""Execute one broker iteration using a single command and frame identity owner."""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from sentinel_contracts.analytics import AdaptiveThresholds, AnalyticsSnapshotRequest
from sentinel_contracts.streaming_market import MarketBatchSnapshot
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.errors import DurableDecisionPersistenceError
from trading_automaton.services.analytics_frame import AnalyticsFrameService, AnalyticsMetricsCache


class TickPort(Protocol):
    async def run_tick(
        self,
        commands: tuple[AutomationCommand, ...],
        snapshot: MarketBatchSnapshot,
        *,
        is_current: Callable[[AutomationCommand], bool] | None = None,
    ) -> object: ...


class PreparationPort(Protocol):
    async def prepare(self, commands: tuple[AutomationCommand, ...], snapshot: MarketBatchSnapshot) -> None: ...


class PersistenceFailurePort(Protocol):
    async def hold(self, commands: tuple[AutomationCommand, ...]) -> None: ...


class RunBrokerIterationUsecase:
    """Orchestrate one fetch, recovery preparation and durable tick with frame deduplication."""

    def __init__(
        self,
        frames: AnalyticsFrameService,
        tick: TickPort,
        *,
        preparation: PreparationPort,
        metrics: AnalyticsMetricsCache,
        source_id: str,
        fallback: AdaptiveThresholds,
        persistence_failure: PersistenceFailurePort | None = None,
    ) -> None:
        self._frames = frames
        self._tick = tick
        self._preparation = preparation
        self._metrics = metrics
        self._source_id = source_id
        self._fallback = fallback
        self._persistence_failure = persistence_failure
        self._commands: tuple[AutomationCommand, ...] = ()
        self._last_seen: dict[str, tuple[str, str]] = {}

    async def replace_commands(self, commands: tuple[AutomationCommand, ...]) -> None:
        """Replace commands immediately, pruning identities for removed automations."""
        self._commands = commands
        active_ids = {str(item.automation_id) for item in commands}
        self._last_seen = {key: value for key, value in self._last_seen.items() if key in active_ids}

    async def execute(self, *, is_closed: Callable[[], bool]) -> None:
        """Prepare recovery even during outage, then tick only current commands on a fresh unseen frame."""
        commands = self._commands
        if not commands or is_closed():
            return
        request = AnalyticsSnapshotRequest(
            source_id=UUID(self._source_id),
            instrument_ids=tuple(sorted({item.external_instrument_id for item in commands})),
            fallback=self._fallback,
        )
        frame = await self._frames.fetch(request)
        fresh = self._frames.fresh_instruments(frame)
        self._metrics.replace({key: value.metrics for key, value in fresh.items()})
        preliminary = self._frames.market_snapshot(frame, fresh)
        # This includes portfolio reconciliation and runs even during Analytics outage.
        await self._preparation.prepare(commands, preliminary)
        if frame is None or is_closed():
            return
        fresh = self._frames.fresh_instruments(frame)
        identity = (frame.snapshot_id, frame.profile_id)
        current = {item.automation_id: item for item in self._commands}
        selected = tuple(
            item
            for item in commands
            if item.state is AutomationState.IN_WORK
            and item.external_instrument_id in fresh
            and current.get(item.automation_id) == item
            and self._last_seen.get(str(item.automation_id)) != identity
        )
        if not selected:
            return
        snapshot = self._frames.market_snapshot(frame, fresh)
        try:
            await self._tick.run_tick(
                selected,
                snapshot,
                is_current=lambda command: any(current == command for current in self._commands),
            )
        except DurableDecisionPersistenceError:
            if self._persistence_failure is not None:
                await self._persistence_failure.hold(selected)
            raise
        for item in selected:
            self._last_seen[str(item.automation_id)] = identity
