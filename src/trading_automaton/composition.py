"""Trading-worker dependency composition."""

from collections.abc import Callable
from datetime import datetime
from time import sleep
from typing import Any, Protocol
from uuid import UUID

import httpx
from sqlalchemy.orm import sessionmaker

from sentinel_contracts.analytics import AdaptiveThresholds
from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.time import utc_now_ms
from sentinel_contracts.tinvest import resolve_tinvest_endpoint
from sentinel_contracts.trading_facts import AutomationCommand, FactEnvelope
from sentinel_contracts.worker_diagnostics import WorkerDiagnostics
from trading_automaton.adapters.analytics_client import AnalyticsClient
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.adapters.tinvest_broker_session import BrokerSdkSession
from trading_automaton.config import AutomatonSettings, StrategySettings
from trading_automaton.domain.core_contracts import AutomationStatusesResult
from trading_automaton.runtime.analytics_broker import AnalyticsBrokerRuntime, AnalyticsPort
from trading_automaton.runtime.streaming_coordinator import (
    CoordinatorCorePort,
    RuntimePort,
    StreamingRuntimeCoordinator,
)
from trading_automaton.services.account_cash_reservation import AccountCashReservationService
from trading_automaton.services.account_commission_profile import AccountCommissionProfileService
from trading_automaton.services.active_intent_gate import ActiveIntentGateService
from trading_automaton.services.analytics_frame import AnalyticsFrameService, AnalyticsMetricsCache
from trading_automaton.services.automation_lifecycle import WorkerAutomationLifecycleService
from trading_automaton.services.batch_runtime import BatchTradingRuntimeService
from trading_automaton.services.broker_rate_limit import BrokerRateLimitService
from trading_automaton.services.broker_tick_preparation import BrokerTickPreparationService
from trading_automaton.services.business_audit import BusinessAuditService
from trading_automaton.services.decision import TradeDecisionService
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.services.decision_materialization import DecisionMaterializerService
from trading_automaton.services.fact_synchronization import FactSynchronizationService
from trading_automaton.services.lot_ledger import LotLedgerService
from trading_automaton.services.order_book_validation import OrderBookValidationService
from trading_automaton.services.order_dispatch import OrderDispatchService
from trading_automaton.services.order_tracking import OrderTrackingService
from trading_automaton.services.persistence_failure import PersistenceFailureService
from trading_automaton.services.portfolio_state_cache import PortfolioStateCacheService
from trading_automaton.services.position_batch_scheduler import PositionBatchSchedulerService
from trading_automaton.services.position_bootstrap import PositionBootstrapService
from trading_automaton.services.position_consistency import PositionConsistencyService
from trading_automaton.services.position_state_hydration import (
    PositionStateCacheService,
    PositionStateHydrationService,
)
from trading_automaton.services.recovery import RecoveryService
from trading_automaton.services.streaming_batch_tick import StreamingBatchTickService
from trading_automaton.services.streaming_cycle_transition import StreamingCycleTransitionService
from trading_automaton.services.streaming_position_decision import StreamingPositionDecisionService
from trading_automaton.services.trading_cycle import TradingCycleService
from trading_automaton.services.uncertain_intent_reconciliation import (
    UncertainIntentReconciliationService,
)
from trading_automaton.storage.database import create_worker_engine, initialize_worker_schema
from trading_automaton.storage.repository import LocalAutomationRepository
from trading_automaton.usecases.broker_iteration import RunBrokerIterationUsecase
from trading_automaton.usecases.recover_worker_run import RecoverWorkerRunUsecase
from trading_automaton.usecases.synchronize_runtime import (
    CoordinatorSynchronizationPort,
    SynchronizeTradingRuntimeUsecase,
)


class BrokerSessionClosePort(Protocol):
    async def close(self) -> None: ...


class OrderTrackingWaitPort(Protocol):
    async def wait_all(self) -> None: ...


class FactCoordinatorCoreAdapter:
    """Expose baseline typed status through the coordinator projection."""

    def __init__(self, core: CoreClient) -> None:
        self._core = core

    def automation_statuses(self, automation_ids: list[str]) -> AutomationStatusesResult:
        result = self._core.automation_statuses([UUID(item) for item in automation_ids])
        return AutomationStatusesResult(
            automations={
                str(status.automation_id): {
                    "state": status.state.value,
                    "revision": status.revision,
                    "last_sequence_number": status.last_sequence_number,
                }
                for status in result.automations
            },
            missing_automation_ids=tuple(str(item) for item in result.missing_automation_ids),
        )

    def broker_connection(self, broker_id: str) -> BrokerConnection:
        return self._core.broker_connection(broker_id)

    def heartbeat(self, worker_id: str, occurred_at: datetime, diagnostics: WorkerDiagnostics | None = None) -> None:
        self._core.heartbeat(worker_id, occurred_at, diagnostics)


class BrokerRuntimeBundle:
    __slots__ = ("broker_id", "runtime", "session", "tracking")

    def __init__(
        self,
        broker_id: str,
        runtime: RuntimePort,
        session: BrokerSessionClosePort,
        tracking: OrderTrackingWaitPort,
    ) -> None:
        self.broker_id = broker_id
        self.runtime = runtime
        self.session = session
        self.tracking = tracking

    async def close(self) -> None:
        try:
            await self.runtime.close()
            await self.tracking.wait_all()
        except BaseException as primary_error:
            try:
                await self.session.close()
            except BaseException as session_error:
                raise primary_error from session_error
            raise
        else:
            await self.session.close()


async def build_broker_runtime(
    connection: BrokerConnection,
    repository: LocalAutomationRepository,
    *,
    strategy_settings: StrategySettings,
    worker_access_mode: str = "READ_ONLY",
    application_environment: str = "TEST",
    now: Callable[[], datetime],
    session_factory: Callable[[BrokerConnection], Any] = lambda connection: BrokerSdkSession(
        connection.token,
        connection.target,
        environment=connection.environment,
        access_mode=connection.access_mode,
        account_id=connection.account_id,
    ),
    tick_seconds: float = 1.0,
    retry_limit: int = 5,
    analytics_url: str = "http://analytics:8001",
    analytics_client: AnalyticsPort | None = None,
) -> BrokerRuntimeBundle:
    """Assemble a broker bundle whose close releases Analytics and the started SDK session."""
    if connection.environment != application_environment:
        raise ValueError("Worker/Core environment mismatch")
    resolve_tinvest_endpoint(connection.environment, connection.adapter_code, connection.target)
    if worker_access_mode not in {"READ_ONLY", "TRADE"}:
        raise ValueError("Invalid Worker broker access mode")
    if worker_access_mode != connection.access_mode:
        raise ValueError("Core/Worker access mode mismatch")
    if connection.environment == "PROD" and worker_access_mode == "TRADE":
        raise ValueError("PROD TRADE is not enabled in this phase")
    if worker_access_mode == "READ_ONLY":
        if strategy_settings.enabled:
            raise ValueError("READ_ONLY requires strategy disabled")
        if repository.has_active_intents():
            raise ValueError(
                "READ_ONLY cannot start with active intents; "
                "preserve history and reconcile in the original trading contour"
            )
    session = session_factory(connection)
    await session.start()
    portfolio = PortfolioStateCacheService()
    metrics = AnalyticsMetricsCache()
    commission_profiles = AccountCommissionProfileService(repository)
    cash = AccountCashReservationService()
    await cash.restore(repository.list_active_buy_intent_reservations(connection.broker_id))
    position_states = PositionStateCacheService()
    active_intents = ActiveIntentGateService()
    ledger = LotLedgerService(repository)
    business_audit = BusinessAuditService(repository, now=now)
    hydration = PositionStateHydrationService(
        repository,
        portfolio,
        None,
        position_states,
        consistency=PositionConsistencyService(ledger, repository, now=now),
        audit=business_audit,
        settings=strategy_settings,
        now=now,
        prepared_metrics=metrics,
    )
    order_books = OrderBookValidationService()
    decider = StreamingPositionDecisionService(
        commission_profiles,
        cash=cash,
        contexts=DecisionContextService(strategy_settings),
        decisions=TradeDecisionService(),
    )
    scheduler = PositionBatchSchedulerService(
        decider,
        rate_limit=BrokerRateLimitService(capacity=2, refill_per_second=2.0),
        order_books=order_books,
        active_intents=active_intents,
    )
    tracking = OrderTrackingService(
        repository,
        now=now,
        broker_id=connection.broker_id,
        commission_profiles=commission_profiles,
        broker=session,
        portfolio=portfolio,
        audit=business_audit,
        cash=cash,
        active_intents=active_intents,
    )
    dispatcher = OrderDispatchService(
        repository,
        session,
        now=now,
        audit=business_audit,
        access_mode=worker_access_mode,
        account_id=connection.account_id,
    )
    batch = BatchTradingRuntimeService(
        repository,
        dispatcher,
        tracking,
        now=now,
        cash=cash,
        active_intents=active_intents,
        access_mode=worker_access_mode,
        account_id=connection.account_id,
    )
    tick = StreamingBatchTickService(
        scheduler,
        position_states,
        batch,
        cash=cash,
        cycles=StreamingCycleTransitionService(now=now, cycles=TradingCycleService(), order_books=order_books),
        now=now,
        audit=business_audit,
        materializer=DecisionMaterializerService(settings=strategy_settings),
        order_books=order_books,
    )
    preparation = BrokerTickPreparationService(
        session,
        portfolio,
        None,
        commission_profiles,
        hydration,
        position_bootstrap=PositionBootstrapService(repository),
        strategy_settings=strategy_settings,
        reconciliation=UncertainIntentReconciliationService(
            repository,
            session,
            now=now,
            audit=business_audit,
            active_intents=active_intents,
            cash=cash,
        ),
        cash=cash,
        now=now,
    )
    analytics = analytics_client or AnalyticsClient(httpx.AsyncClient(base_url=analytics_url, timeout=1.0))
    iteration = RunBrokerIterationUsecase(
        AnalyticsFrameService(analytics, now=now, order_books=order_books),
        tick,
        preparation=preparation,
        metrics=metrics,
        source_id=connection.broker_id,
        fallback=AdaptiveThresholds(
            strategy_settings.averaging_step_percent,
            strategy_settings.partial_take_profit_percent,
            "STRATEGY",
        ),
        persistence_failure=PersistenceFailureService(repository),
    )
    runtime = AnalyticsBrokerRuntime(
        analytics,
        iteration,
        tick_seconds=tick_seconds,
        retry_limit=retry_limit,
        account_id=connection.account_id,
    )
    return BrokerRuntimeBundle(connection.broker_id, runtime, session, tracking)


def build_worker_recovery(repository: LocalAutomationRepository, worker_id: str) -> RecoverWorkerRunUsecase:
    """Compose startup recovery around the same durable repository used by the runtime."""
    return RecoverWorkerRunUsecase(repository, RecoveryService(repository), worker_id=worker_id)


def build_streaming_runtime(
    settings: AutomatonSettings,
    strategy_settings: StrategySettings,
) -> tuple[
    StreamingRuntimeCoordinator,
    LocalAutomationRepository,
    httpx.Client,
]:
    """Return the coordinator, repository and Core HTTP client.

    The caller closes the coordinator, finishes its run marker and closes HTTP."""
    if settings.broker_access_mode == "READ_ONLY" and strategy_settings.enabled:
        raise ValueError("READ_ONLY requires strategy disabled")
    engine = create_worker_engine(settings.database_url)
    initialize_worker_schema(engine)
    repository = LocalAutomationRepository(sessionmaker(engine, expire_on_commit=False))
    if settings.broker_access_mode == "READ_ONLY" and repository.has_active_intents():
        engine.dispose()
        raise ValueError(
            "READ_ONLY cannot start with active intents; preserve history and reconcile in the original trading contour"
        )
    http = httpx.Client(base_url=settings.core_url, timeout=10.0)
    core = CoreClient(
        http, application_environment=settings.application_environment, access_mode=settings.broker_access_mode
    )

    def now() -> datetime:
        return utc_now_ms()

    def validate_commands(commands: list[AutomationCommand]) -> None:
        for broker_id in {str(command.broker_id) for command in commands}:
            connection = core.broker_connection(broker_id)
            if connection.environment != settings.application_environment:
                raise ValueError("Worker/Core environment mismatch")
            if connection.access_mode != settings.broker_access_mode:
                raise ValueError("Worker/Core access mode mismatch")
            if connection.account_id and any(
                str(command.broker_id) == broker_id and command.account_id != connection.account_id
                for command in commands
            ):
                raise ValueError("Broker command account mismatch")

    def validate_facts(facts: list[FactEnvelope]) -> None:
        scopes = {broker_id: core.broker_scope(broker_id) for broker_id in {str(fact.user_broker_id) for fact in facts}}
        commands = {
            automation_id: repository.get_cached_command(automation_id)
            for automation_id in {str(fact.automation_id) for fact in facts}
        }
        for fact in facts:
            broker_id = str(fact.user_broker_id)
            scope = scopes[broker_id]
            command = commands[str(fact.automation_id)]
            if scope.broker_id != broker_id or str(command.user_broker_id) != broker_id:
                raise ValueError("Fact delivery broker scope mismatch")
            if scope.environment != settings.application_environment:
                raise ValueError("Worker/Core environment mismatch")
            if scope.account_id != command.account_id:
                raise ValueError("Fact delivery account mismatch")

    synchronization: CoordinatorSynchronizationPort = FactSynchronizationService(
        repository,
        core,
        now=now,
        sleep=sleep,
        retry_limit=strategy_settings.core_retry_limit,
        batch_size=settings.fact_outbox_batch_size,
        deadline_ms=settings.fact_outbox_deadline_ms,
        validate_commands=validate_commands,
        validate_facts=validate_facts,
    )
    coordinator_core: CoordinatorCorePort = FactCoordinatorCoreAdapter(core)

    async def builder(connection: BrokerConnection) -> BrokerRuntimeBundle:
        return await build_broker_runtime(
            connection,
            repository,
            strategy_settings=strategy_settings,
            worker_access_mode=settings.broker_access_mode,
            application_environment=settings.application_environment,
            now=now,
            tick_seconds=settings.iteration_seconds,
            retry_limit=settings.sandbox_retry_limit,
            analytics_url=settings.analytics_url,
        )

    iteration = SynchronizeTradingRuntimeUsecase(
        repository,
        synchronization,
        coordinator_core,
        WorkerAutomationLifecycleService(repository),
        worker_id=settings.worker_id,
        now=now,
    )
    runtime = StreamingRuntimeCoordinator(
        iteration,
        coordinator_core,
        builder,
        worker_id=settings.worker_id,
        now=now,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        outbox_diagnostics=repository.outbox_diagnostics,
    )
    return runtime, repository, http
