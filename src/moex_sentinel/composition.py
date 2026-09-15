"""Composition root for concrete application dependencies."""

from typing import cast
from uuid import UUID

from pydantic import ConfigDict
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.adapters.registry import BrokerAdapterRegistry
from moex_sentinel.adapters.tinvest.market_data import TInvestMarketDataAdapter
from moex_sentinel.adapters.tinvest.market_stream_source import TInvestMarketStreamSource
from moex_sentinel.adapters.tinvest.order_execution import TInvestOrderExecutionAdapter
from moex_sentinel.adapters.tinvest.portfolio import TInvestPortfolioAdapter
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
from moex_sentinel.services.instrument_catalog import InstrumentCatalogService
from moex_sentinel.services.market_data import BrokerMarketDataService
from moex_sentinel.services.market_snapshot_gateway import MarketSnapshotGateway
from moex_sentinel.services.portfolio import PortfolioAggregationService
from moex_sentinel.services.portfolio_snapshot_collection import PortfolioSnapshotCollectionService
from moex_sentinel.services.position_adoption import ConfiguredPositionAdoptionService, PositionAdoptionService
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.services.trading_sessions import TradingSessionService
from moex_sentinel.services.trading_summary import TradingSummaryService
from moex_sentinel.storage.portfolio_snapshot_collection import PortfolioSnapshotCollectionStore
from moex_sentinel.storage.repositories.automation_commands import AutomationCommandRepository
from moex_sentinel.storage.repositories.automations import AutomationRepository
from moex_sentinel.storage.repositories.portfolio_snapshots import PortfolioSnapshotRepository
from moex_sentinel.storage.repositories.position_adoption import PositionAdoptionRepository
from moex_sentinel.storage.repositories.reference_catalog import ReferenceCatalogRepository
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from moex_sentinel.usecases.automations import (
    CloseAutomationUsecase,
    CreateTradingAutomationUsecase,
    HoldAutomationUsecase,
    ResumeAutomationUsecase,
    ViewTradingAutomationDetailsUsecase,
    ViewTradingAutomationStatusesUsecase,
    ViewTradingAutomationsUsecase,
    ViewTradingAutomationUsecase,
)
from moex_sentinel.usecases.automaton_brokers import ViewAutomatonBrokerConnectionUsecase
from moex_sentinel.usecases.automaton_sync import (
    RecordAutomatonHeartbeatUsecase,
)
from moex_sentinel.usecases.brokers import (
    DeleteBrokerSettingsUsecase,
    SaveBrokerSettingsUsecase,
    ViewBrokerSettingsUsecase,
)
from moex_sentinel.usecases.connections import CheckBrokerConnectionUsecase
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
    view_automaton_broker_connection: ViewAutomatonBrokerConnectionUsecase
    record_automaton_heartbeat: RecordAutomatonHeartbeatUsecase
    create_trading_automation: CreateTradingAutomationUsecase
    view_trading_automation: ViewTradingAutomationUsecase
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


class _SessionTradingSummaryService:
    """Keep one consistent database session for all summary reads."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def view(self) -> TradingSummaryView:
        with self._factory() as session:
            return TradingSummaryService(PortfolioSnapshotRepository(session)).view()


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
        retry_limit=settings.portfolio_snapshot_retry_limit,
        retry_base_seconds=settings.portfolio_snapshot_retry_base_seconds,
    )
    return CollectPortfolioSnapshotsUsecase(collection, store, clock=clock)


def build_market_snapshot_gateway(factory: sessionmaker[Session], *, retry_limit: int = 5) -> MarketSnapshotGateway:
    """Revalidate Core-owned configuration on each read and rotate changed sessions."""
    connections = AutomatonBrokerService(UserBrokerRepository(factory))

    def source(configuration: object) -> TInvestMarketStreamSource:
        connection = cast(BrokerConnection, configuration)
        return TInvestMarketStreamSource(connection.token, connection.target)

    def resolve(source_id: UUID) -> BrokerConnection:
        return connections.connection(str(source_id))

    return MarketSnapshotGateway(source, configuration_resolver=resolve, retry_limit=retry_limit)


def build_application_usecases(factory: sessionmaker[Session]) -> ApplicationUsecases:
    """Assemble application actors sharing configured services and the supplied session factory."""
    repository = UserBrokerRepository(factory)
    registry = BrokerAdapterRegistry()
    broker_service = BrokerConfigurationService(repository, registry)
    adapter_factory = PortfolioAdapterFactory(TInvestPortfolioAdapter)
    connection_service = BrokerConnectionService(repository, adapter_factory.create)
    instrument_repository = ReferenceCatalogRepository(factory)
    portfolio_service = PortfolioAggregationService(repository, adapter_factory.create, None, instrument_repository)
    market_data_factory = MarketDataAdapterFactory(TInvestMarketDataAdapter)
    market_data_service = BrokerMarketDataService(repository, market_data_factory.create)
    position_adoption = ConfiguredPositionAdoptionService(
        PositionAdoptionService(PositionAdoptionRepository(factory)),
        repository.get,
        lambda broker: TInvestOrderExecutionAdapter(
            str(broker.settings.get("token", "")),
            broker.fqdn,
        ),
    )
    instrument_catalog_service = InstrumentCatalogService(
        repository,
        instrument_repository,
        market_data_factory.create,
    )
    automation_repository = AutomationRepository(factory)
    automation_service = AutomationService(automation_repository)
    automaton_sync_service = AutomatonSyncService()
    automaton_broker_service = AutomatonBrokerService(repository)
    trading_session_service = TradingSessionService(
        automation_service,
        automaton_broker_service,
        lambda connection: TInvestOrderExecutionAdapter(connection.token, connection.target),
        instrument_repository,
    )
    automation_commands = AutomationCommandRepository(factory)
    trading_fact_ingress = TradingFactIngressService(
        lambda: TradingFactsUnitOfWork(factory),
        TradingFactMapper(),
    )
    return ApplicationUsecases(
        view_automaton_broker_connection=ViewAutomatonBrokerConnectionUsecase(automaton_broker_service),
        record_automaton_heartbeat=RecordAutomatonHeartbeatUsecase(automaton_sync_service),
        create_trading_automation=CreateTradingAutomationUsecase(automation_service),
        view_trading_automation=ViewTradingAutomationUsecase(automation_service),
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
        view_trading_summary=ViewTradingSummaryUsecase(_SessionTradingSummaryService(factory)),
    )
