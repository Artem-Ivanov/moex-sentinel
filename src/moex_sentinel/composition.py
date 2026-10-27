"""Composition root for concrete application dependencies."""

from typing import cast
from uuid import UUID

from pydantic import ConfigDict
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.adapters.registry import BrokerAdapterRegistry
from moex_sentinel.adapters.tinvest.admission_risk import TInvestAdmissionRiskAdapter
from moex_sentinel.adapters.tinvest.market_data import TInvestMarketDataAdapter
from moex_sentinel.adapters.tinvest.market_stream_source import TInvestMarketStreamSource
from moex_sentinel.adapters.tinvest.order_execution import TInvestOrderExecutionAdapter
from moex_sentinel.adapters.tinvest.portfolio import TInvestPortfolioAdapter
from moex_sentinel.api.worker_auth import WorkerAuthenticator
from moex_sentinel.config import Settings
from moex_sentinel.domain.trading_summary import TradingSummaryView
from moex_sentinel.services.automations import AutomationService
from moex_sentinel.services.automaton_brokers import AutomatonBrokerService
from moex_sentinel.services.automaton_sync import AutomatonSyncService
from moex_sentinel.services.broker_factory import (
    MarketDataAdapterFactory,
    PortfolioAdapterFactory,
)
from moex_sentinel.services.brokers import BrokerConfigurationService
from moex_sentinel.services.connections import BrokerConnectionService
from moex_sentinel.services.daily_admission_preparation import DailyAdmissionPreparationService
from moex_sentinel.services.environment import PinnedEnvironment
from moex_sentinel.services.execution_commissions import (
    ExecutionCommissionCollectionService,
    ExecutionCommissionService,
)
from moex_sentinel.services.instrument_catalog import InstrumentCatalogService
from moex_sentinel.services.market_data import BrokerMarketDataService
from moex_sentinel.services.market_recovery import MarketRecoveryPolicy
from moex_sentinel.services.market_snapshot_gateway import MarketSnapshotGateway
from moex_sentinel.services.portfolio import PortfolioAggregationService
from moex_sentinel.services.portfolio_snapshot_collection import PortfolioSnapshotCollectionService
from moex_sentinel.services.position_adoption import ConfiguredPositionAdoptionService, PositionAdoptionService
from moex_sentinel.services.trading_admission import TradingAdmissionService
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.services.trading_sessions import TradingSessionService
from moex_sentinel.services.trading_summary import TradingSummaryService
from moex_sentinel.storage.portfolio_snapshot_collection import PortfolioSnapshotCollectionStore
from moex_sentinel.storage.repositories.automation_commands import AutomationCommandRepository
from moex_sentinel.storage.repositories.automations import AutomationRepository
from moex_sentinel.storage.repositories.execution_commissions import ExecutionCommissionRepository
from moex_sentinel.storage.repositories.portfolio_snapshots import PortfolioSnapshotRepository
from moex_sentinel.storage.repositories.position_adoption import PositionAdoptionRepository
from moex_sentinel.storage.repositories.reference_catalog import ReferenceCatalogRepository
from moex_sentinel.storage.repositories.trading_admission import TradingAdmissionRepository
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from moex_sentinel.usecases.automations import (
    CloseAutomationUsecase,
    CreateTradingAutomationUsecase,
    HoldAutomationUsecase,
    ResumeAutomationUsecase,
    UpdateTradingAutomationSettingsUsecase,
    ViewTradingAutomationDecisionReasonsUsecase,
    ViewTradingAutomationDetailsUsecase,
    ViewTradingAutomationStatusesUsecase,
    ViewTradingAutomationsUsecase,
    ViewTradingAutomationUsecase,
)
from moex_sentinel.usecases.automaton_brokers import (
    ViewAutomatonBrokerConnectionUsecase,
    ViewAutomatonBrokerScopeUsecase,
)
from moex_sentinel.usecases.automaton_sync import (
    RecordAutomatonHeartbeatUsecase,
)
from moex_sentinel.usecases.brokers import (
    DeleteBrokerSettingsUsecase,
    SaveBrokerSettingsUsecase,
    ViewBrokerSettingsUsecase,
)
from moex_sentinel.usecases.connections import CheckBrokerConnectionUsecase
from moex_sentinel.usecases.execution_diagnostics import ViewExecutionDiagnosticsUsecase
from moex_sentinel.usecases.instruments import (
    SetInstrumentSelectionUsecase,
    SynchronizeBrokerInstrumentsUsecase,
    ViewBrokerInstrumentsUsecase,
    ViewInstrumentDetailsUsecase,
)
from moex_sentinel.usecases.market_data import (
    SearchMarketInstrumentsUsecase,
    ViewHistoricCandlesUsecase,
    ViewMarketInstrumentUsecase,
)
from moex_sentinel.usecases.portfolio import (
    ViewBrokerAccountsUsecase,
    ViewOpenPositionsUsecase,
    ViewPortfolioSummaryUsecase,
    ViewRecentOperationsUsecase,
)
from moex_sentinel.usecases.portfolio_snapshots import CollectPortfolioSnapshotsUsecase
from moex_sentinel.usecases.trading_fact_ingress import (
    ClaimAutomationCommandsUsecase,
    PublishTradingFactsUsecase,
    ViewAutomationStatusesUsecase,
)
from moex_sentinel.usecases.trading_sessions import ViewTradingSessionsStatusUsecase
from moex_sentinel.usecases.trading_summary import ViewTradingSummaryUsecase
from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import BrokerConnection
from sentinel_contracts.time import utc_now_ms


class ApplicationUsecases(PositionalModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    view_automaton_broker_scope: ViewAutomatonBrokerScopeUsecase
    view_automaton_broker_connection: ViewAutomatonBrokerConnectionUsecase
    record_automaton_heartbeat: RecordAutomatonHeartbeatUsecase
    create_trading_automation: CreateTradingAutomationUsecase
    update_trading_automation_settings: UpdateTradingAutomationSettingsUsecase
    view_trading_automation: ViewTradingAutomationUsecase
    view_execution_diagnostics: ViewExecutionDiagnosticsUsecase
    view_trading_automation_decision_reasons: ViewTradingAutomationDecisionReasonsUsecase
    view_trading_automation_statuses: ViewTradingAutomationStatusesUsecase
    view_trading_automation_details: ViewTradingAutomationDetailsUsecase
    view_trading_automations: ViewTradingAutomationsUsecase
    hold_automation: HoldAutomationUsecase
    resume_automation: ResumeAutomationUsecase
    close_automation: CloseAutomationUsecase
    view_trading_sessions_status: ViewTradingSessionsStatusUsecase
    view_broker_settings: ViewBrokerSettingsUsecase
    save_broker_settings: SaveBrokerSettingsUsecase
    delete_broker_settings: DeleteBrokerSettingsUsecase
    check_broker_connection: CheckBrokerConnectionUsecase
    view_broker_accounts: ViewBrokerAccountsUsecase
    view_portfolio_summary: ViewPortfolioSummaryUsecase
    view_external_positions: ViewOpenPositionsUsecase
    view_recent_operations: ViewRecentOperationsUsecase
    search_market_instruments: SearchMarketInstrumentsUsecase
    view_market_instrument: ViewMarketInstrumentUsecase
    view_historic_candles: ViewHistoricCandlesUsecase
    synchronize_broker_instruments: SynchronizeBrokerInstrumentsUsecase
    view_broker_instruments: ViewBrokerInstrumentsUsecase
    view_instrument_details: ViewInstrumentDetailsUsecase
    set_instrument_selection: SetInstrumentSelectionUsecase
    claim_automation_commands: ClaimAutomationCommandsUsecase
    view_automation_statuses: ViewAutomationStatusesUsecase
    publish_trading_facts: PublishTradingFactsUsecase
    view_trading_summary: ViewTradingSummaryUsecase
    trading_admission: TradingAdmissionService
    daily_admission_preparation: DailyAdmissionPreparationService
    worker_authenticator: WorkerAuthenticator


class _SessionTradingSummaryService:
    """Keep one consistent database session for all summary reads."""

    def __init__(self, factory: sessionmaker[Session], environment: str) -> None:
        self._factory = factory
        self._environment = environment

    def view(self) -> TradingSummaryView:
        with self._factory() as session:
            return TradingSummaryService(PortfolioSnapshotRepository(session, environment=self._environment)).view()


def build_portfolio_snapshot_collector(
    factory: sessionmaker[Session],
    engine: Engine,
    settings: Settings,
    *,
    clock=utc_now_ms,
) -> CollectPortfolioSnapshotsUsecase:
    """Build one portfolio collection operation with short sessions and a run lock."""
    repository = UserBrokerRepository(factory)
    adapter_factory = PortfolioAdapterFactory(TInvestPortfolioAdapter)
    store = PortfolioSnapshotCollectionStore(factory, engine)
    collection = PortfolioSnapshotCollectionService(
        repository,
        adapter_factory.create,
        store,
        clock=clock,
        environment=(
            None
            if settings.portfolio_snapshot_all_environments
            else PinnedEnvironment(settings.application_environment)
        ),
        retry_limit=settings.portfolio_snapshot_retry_limit,
        retry_base_seconds=settings.portfolio_snapshot_retry_base_seconds,
    )
    return CollectPortfolioSnapshotsUsecase(collection, store, clock=clock)


def build_execution_commission_collector(factory: sessionmaker[Session], *, clock=utc_now_ms):
    """Build the independent, one-page PROD broker fee collector."""
    brokers = UserBrokerRepository(factory)
    return ExecutionCommissionCollectionService(
        ExecutionCommissionService(ExecutionCommissionRepository(factory), now=clock),
        brokers,
        PortfolioAdapterFactory(TInvestPortfolioAdapter).create,
    )


def build_market_snapshot_gateway(
    factory: sessionmaker[Session], *, retry_limit: int = 5, settings: Settings | None = None
) -> MarketSnapshotGateway:
    """Revalidate Core-owned configuration on each read and rotate changed sessions."""
    settings = settings or Settings()
    connections = AutomatonBrokerService(
        UserBrokerRepository(factory),
        access_mode=settings.broker_access_mode,
        environment=PinnedEnvironment(settings.application_environment),
    )

    def source(configuration: object) -> TInvestMarketStreamSource:
        connection = cast(BrokerConnection, configuration)
        return TInvestMarketStreamSource(connection.token, connection.target)

    def resolve(source_id: UUID) -> BrokerConnection:
        return connections.connection(str(source_id))

    recovery = MarketRecoveryPolicy(
        retry_seconds=settings.market_recovery_retry_base_seconds,
        retry_limit=retry_limit,
        probe_interval_seconds=settings.market_recovery_probe_interval_seconds,
        operation_timeout_seconds=settings.market_operation_timeout_seconds,
        close_timeout_seconds=settings.market_close_timeout_seconds,
        quiet_seconds=settings.market_quiet_seconds,
    )
    return MarketSnapshotGateway(
        source,
        configuration_resolver=resolve,
        retry_limit=retry_limit,
        refresh_seconds=settings.market_candles_refresh_seconds,
        trading_status_ttl_seconds=settings.market_trading_status_ttl_seconds,
        recovery=recovery,
    )


def build_application_usecases(factory: sessionmaker[Session], settings: Settings | None = None) -> ApplicationUsecases:
    """Assemble application actors sharing configured services and the supplied session factory."""
    settings = settings or Settings()
    environment = PinnedEnvironment(settings.application_environment)
    repository = UserBrokerRepository(factory)
    registry = BrokerAdapterRegistry()
    broker_service = BrokerConfigurationService(repository, registry, environment)
    adapter_factory = PortfolioAdapterFactory(TInvestPortfolioAdapter)
    connection_service = BrokerConnectionService(repository, adapter_factory.create, environment)
    instrument_repository = ReferenceCatalogRepository(factory)
    portfolio_service = PortfolioAggregationService(
        repository, adapter_factory.create, environment, instrument_repository
    )
    market_data_factory = MarketDataAdapterFactory(TInvestMarketDataAdapter)
    market_data_service = BrokerMarketDataService(repository, market_data_factory.create, environment)
    position_adoption = ConfiguredPositionAdoptionService(
        PositionAdoptionService(PositionAdoptionRepository(factory)),
        repository.get,
        lambda broker: TInvestOrderExecutionAdapter(
            str(broker.settings.get("token", "")),
            broker.fqdn,
            account_id=broker.account_id or "",
        ),
    )
    instrument_catalog_service = InstrumentCatalogService(
        repository,
        instrument_repository,
        market_data_factory.create,
        environment,
    )
    automation_repository = AutomationRepository(factory)
    automation_service = AutomationService(
        automation_repository, access_mode=settings.broker_access_mode, environment=environment, brokers=repository
    )
    automaton_sync_service = AutomatonSyncService()
    automaton_broker_service = AutomatonBrokerService(
        repository, access_mode=settings.broker_access_mode, environment=environment
    )
    trading_session_service = TradingSessionService(
        automation_service,
        automaton_broker_service,
        lambda connection: TInvestOrderExecutionAdapter(
            connection.token,
            connection.target,
            access_mode="READ_ONLY" if connection.environment == "PROD" else connection.access_mode,
            account_id=connection.account_id,
        ),
        instrument_repository,
    )
    automation_commands = AutomationCommandRepository(factory, environment=settings.application_environment)
    trading_fact_ingress = TradingFactIngressService(
        lambda: TradingFactsUnitOfWork(factory),
        TradingFactMapper(),
        environment=settings.application_environment,
    )
    trading_admission_repository = TradingAdmissionRepository(
        factory, application_environment=settings.application_environment
    )
    trading_admission = TradingAdmissionService(
        trading_admission_repository,
        risk=TInvestAdmissionRiskAdapter(),
        connection_resolver=automaton_broker_service.connection,
    )
    daily_admission_preparation = DailyAdmissionPreparationService(
        trading_admission,
        broker_id=None if settings.internal_worker_broker_id is None else str(settings.internal_worker_broker_id),
        enabled=settings.daily_admission_preparation_enabled,
        environment=settings.application_environment,
        access_mode=settings.broker_access_mode,
        check_interval_seconds=settings.daily_admission_check_interval_seconds,
        attempt_timeout_seconds=settings.daily_admission_attempt_timeout_seconds,
        retry_delays_seconds=settings.daily_admission_retry_delays_seconds,
    )
    worker_authenticator = WorkerAuthenticator(settings, trading_admission_repository.get_runtime_credential)
    view_trading_automation = ViewTradingAutomationUsecase(automation_service)
    return ApplicationUsecases(
        view_automaton_broker_scope=ViewAutomatonBrokerScopeUsecase(automaton_broker_service),
        view_automaton_broker_connection=ViewAutomatonBrokerConnectionUsecase(automaton_broker_service),
        record_automaton_heartbeat=RecordAutomatonHeartbeatUsecase(automaton_sync_service),
        create_trading_automation=CreateTradingAutomationUsecase(automation_service),
        update_trading_automation_settings=UpdateTradingAutomationSettingsUsecase(automation_service),
        view_trading_automation=view_trading_automation,
        view_execution_diagnostics=ViewExecutionDiagnosticsUsecase(
            view_trading_automation, lambda: TradingFactsUnitOfWork(factory)
        ),
        view_trading_automation_decision_reasons=ViewTradingAutomationDecisionReasonsUsecase(
            view_trading_automation,
            lambda: TradingFactsUnitOfWork(factory),
        ),
        view_trading_automation_statuses=ViewTradingAutomationStatusesUsecase(automation_service),
        view_trading_automation_details=ViewTradingAutomationDetailsUsecase(
            automation_service, portfolio_service, instrument_catalog_service
        ),
        view_trading_automations=ViewTradingAutomationsUsecase(automation_service),
        hold_automation=HoldAutomationUsecase(automation_service),
        resume_automation=ResumeAutomationUsecase(automation_service),
        close_automation=CloseAutomationUsecase(automation_service),
        view_trading_sessions_status=ViewTradingSessionsStatusUsecase(trading_session_service),
        view_broker_settings=ViewBrokerSettingsUsecase(broker_service),
        save_broker_settings=SaveBrokerSettingsUsecase(broker_service),
        delete_broker_settings=DeleteBrokerSettingsUsecase(broker_service),
        check_broker_connection=CheckBrokerConnectionUsecase(connection_service),
        view_broker_accounts=ViewBrokerAccountsUsecase(portfolio_service),
        view_portfolio_summary=ViewPortfolioSummaryUsecase(portfolio_service),
        view_external_positions=ViewOpenPositionsUsecase(portfolio_service),
        view_recent_operations=ViewRecentOperationsUsecase(portfolio_service),
        search_market_instruments=SearchMarketInstrumentsUsecase(market_data_service),
        view_market_instrument=ViewMarketInstrumentUsecase(market_data_service),
        view_historic_candles=ViewHistoricCandlesUsecase(market_data_service),
        synchronize_broker_instruments=SynchronizeBrokerInstrumentsUsecase(
            instrument_catalog_service,
            position_adoption,
        ),
        view_broker_instruments=ViewBrokerInstrumentsUsecase(instrument_catalog_service),
        view_instrument_details=ViewInstrumentDetailsUsecase(instrument_catalog_service),
        set_instrument_selection=SetInstrumentSelectionUsecase(instrument_catalog_service),
        claim_automation_commands=ClaimAutomationCommandsUsecase(automation_commands),
        view_automation_statuses=ViewAutomationStatusesUsecase(automation_commands),
        publish_trading_facts=PublishTradingFactsUsecase(trading_fact_ingress),
        view_trading_summary=ViewTradingSummaryUsecase(
            _SessionTradingSummaryService(factory, settings.application_environment)
        ),
        trading_admission=trading_admission,
        daily_admission_preparation=daily_admission_preparation,
        worker_authenticator=worker_authenticator,
    )
