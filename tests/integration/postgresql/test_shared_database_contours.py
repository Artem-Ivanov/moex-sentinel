"""One database: contour isolation of mutable scopes and durable projections."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from moex_sentinel.composition import build_application_usecases, build_portfolio_snapshot_collector
from moex_sentinel.config import Settings
from moex_sentinel.domain.brokers import BrokerDraft, BrokerField
from moex_sentinel.domain.portfolio import BrokerAccount, BrokerReadError
from moex_sentinel.domain.trading_summary import PortfolioSnapshotRunValue, PortfolioSnapshotValue
from moex_sentinel.domain.user_brokers import UserBrokerDraft, UserBrokerNotFoundError
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.services.trading_summary import TradingSummaryService
from moex_sentinel.storage.database import create_database_engine, create_session_factory
from moex_sentinel.storage.models import Base, TradingAutomationModel, UserBrokerModel
from moex_sentinel.storage.repositories.automation_commands import AutomationCommandRepository
from moex_sentinel.storage.repositories.portfolio_snapshots import PortfolioSnapshotRepository
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository
from moex_sentinel.usecases.errors import UseCaseError
from sentinel_contracts.trading_facts import FactIngressErrorCode
from tests.services.test_portfolio_snapshot_collection import PortfolioAdapter
from tests.services.test_trading_fact_ingress import state_fact
from tests.storage.trading_facts_helpers import automation_model, instrument_model, user_broker_model

NOW = datetime(2026, 8, 15, 10, tzinfo=UTC)


def append_run(repository, captured_at, values, *, errors=()):
    run_id = str(uuid4())
    run = PortfolioSnapshotRunValue(
        run_id, captured_at, captured_at.replace(second=0, microsecond=0), errors, captured_at
    )
    snapshots = tuple(
        PortfolioSnapshotValue(
            id=str(uuid4()),
            run_id=run_id,
            user_broker_id=broker_id,
            account_id=account_id,
            currency=currency,
            total_value=Decimal(total),
            free_cash=Decimal(free_cash),
            cumulative_pnl=Decimal(pnl),
            captured_at=captured_at,
            bucket_start=run.bucket_start,
            created_at=captured_at,
        )
        for broker_id, account_id, currency, total, free_cash, pnl in values
    )
    repository.append_run_with_snapshots(run, snapshots)


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def shared_database(request, tmp_path):
    url = (
        f"sqlite:///{tmp_path / 'contours.db'}"
        if request.param == "sqlite"
        else request.getfixturevalue("isolated_postgresql_database_url")
    )
    engine = create_database_engine(url)
    if request.param == "sqlite":
        Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    scopes = {}
    with factory.begin() as session:
        for environment in ("TEST", "PROD"):
            broker_id, instrument_id, automation_id = (str(uuid4()) for _ in range(3))
            broker = user_broker_model(broker_id, f"account-{environment}")
            broker.environment = environment
            broker.fqdn = (
                "sandbox-invest-public-api.tbank.ru:443" if environment == "TEST" else "invest-public-api.tbank.ru:443"
            )
            session.add(broker)
            session.flush()
            instrument = instrument_model(instrument_id, broker_id)
            instrument.ticker = environment
            session.add(instrument)
            session.flush()
            session.add(automation_model(automation_id, user_broker_id=broker_id, instrument_id=instrument_id))
            scopes[environment] = (broker_id, automation_id)
    try:
        yield factory, scopes
    finally:
        engine.dispose()


@pytest.mark.parametrize("state", ["DRAFT", "DISABLED", "ACTIVE"])
@pytest.mark.parametrize("operation", ["replace", "disable"])
def test_foreign_mutation_rejected_without_change(shared_database, state, operation):
    factory, scopes = shared_database
    broker_id, _ = scopes["PROD"]
    with factory.begin() as session:
        model = session.get(UserBrokerModel, broker_id)
        model.state = state
        model.external_account_id = None if state == "DRAFT" else model.external_account_id
    repository = UserBrokerRepository(factory)
    before = repository.get(broker_id)
    draft = UserBrokerDraft.model_validate(before.model_dump(exclude={"id", "created_at", "updated_at", "archived_at"}))
    draft = draft.model_copy(update={"display_name": "changed by foreign backend"})
    if operation == "replace":
        with pytest.raises(UserBrokerNotFoundError):
            repository.replace(broker_id, draft, expected_environment="TEST")
    else:
        with pytest.raises(UserBrokerNotFoundError):
            repository.disable(broker_id, expected_environment="TEST")
    assert repository.get(broker_id) == before


def test_commands_and_statuses_only_expose_pinned_contour(shared_database):
    factory, scopes = shared_database
    with factory.begin() as session:
        for _, automation_id in scopes.values():
            session.get(TradingAutomationModel, automation_id).state = "IN_QUEUE"
    for environment in ("TEST", "PROD"):
        repository = AutomationCommandRepository(factory, environment=environment)
        own_id = UUID(scopes[environment][1])
        foreign_id = UUID(scopes["PROD" if environment == "TEST" else "TEST"][1])
        assert [command.automation_id for command in repository.claim(100)] == [own_id]
        assert [command.automation_id for command in repository.claim(1)] == [own_id]
        result = repository.statuses([foreign_id, own_id, foreign_id])
        assert [status.automation_id for status in result.automations] == [own_id]
        assert result.missing_automation_ids == (foreign_id,)


def test_foreign_fact_rejected_before_replay_ack_and_sequence_change(shared_database):
    factory, scopes = shared_database
    broker_id, automation_id = scopes["PROD"]
    fact = state_fact(UUID(automation_id), event_id=uuid4()).model_copy(update={"user_broker_id": UUID(broker_id)})
    prod = TradingFactIngressService(lambda: TradingFactsUnitOfWork(factory), TradingFactMapper(), environment="PROD")
    test = TradingFactIngressService(lambda: TradingFactsUnitOfWork(factory), TradingFactMapper(), environment="TEST")
    rejected = test.publish([fact])
    assert rejected.results == ()
    assert rejected.failures[0].code == FactIngressErrorCode.CROSS_SCOPE_RELATION
    assert rejected.failures[0].retryable is False
    with factory() as session:
        assert session.get(TradingAutomationModel, automation_id).last_sequence_number == 0
    assert len(prod.publish([fact]).results) == 1
    replay = test.publish([fact])
    assert replay.results == ()
    assert replay.failures[0].code == FactIngressErrorCode.CROSS_SCOPE_RELATION
    with factory() as session:
        assert session.get(TradingAutomationModel, automation_id).last_sequence_number == 1


def test_summary_and_baselines_exclude_other_contour(shared_database):
    factory, scopes = shared_database
    with factory.begin() as session:
        repository = PortfolioSnapshotRepository(session)
        for captured, values in ((NOW - timedelta(days=2), ("100", "10000")), (NOW, ("110", "12000"))):
            append_run(
                repository,
                captured,
                tuple(
                    (scopes[environment][0], f"account-{environment}", "RUB", value, value, "0")
                    for environment, value in zip(("TEST", "PROD"), values, strict=True)
                ),
            )
    with factory() as session:
        scoped = PortfolioSnapshotRepository(session, environment="TEST")
        result = TradingSummaryService(scoped).view()
        assert result.captured_at == NOW
        assert result.currencies[0].portfolio_value == 110
        own = scoped.latest_snapshots(scoped.latest_run().id)
        assert len(own) == 1
        baselines = scoped.common_baselines(own, NOW - timedelta(days=1))
        assert len(baselines) == 1
        assert baselines[0].user_broker_id == scopes["TEST"][0]
        assert baselines[0].total_value == 100
        foreign_id = scopes["PROD"][0]
        assert scoped.latest(foreign_id, "account-PROD", "RUB") is None
        assert scoped.first(foreign_id, "account-PROD", "RUB") is None
        assert scoped.baseline(foreign_id, "account-PROD", "RUB", NOW) is None
        foreign = PortfolioSnapshotRepository(session).latest(foreign_id, "account-PROD", "RUB")
        assert scoped.common_baselines((foreign,), NOW) == ()
    application = build_application_usecases(factory, settings=Settings(_env_file=None, broker_access_mode="READ_ONLY"))
    assert application.view_trading_summary.execute().currencies[0].portfolio_value == 110


def test_composition_pins_backend_even_when_global_collector_enabled(shared_database):
    factory, scopes = shared_database
    settings = Settings(_env_file=None, broker_access_mode="READ_ONLY", portfolio_snapshot_all_environments=True)
    application = build_application_usecases(factory, settings=settings)
    assert {broker.id for broker in application.view_broker_settings.execute().brokers} == {scopes["TEST"][0]}
    with factory.begin() as session:
        for _, automation_id in scopes.values():
            session.get(TradingAutomationModel, automation_id).state = "IN_QUEUE"
    # Internal delivery endpoints use the same pinned composition as the public API.
    assert {command.user_broker_id for command in application.claim_automation_commands.execute(100)} == {
        UUID(scopes["TEST"][0])
    }


@pytest.mark.parametrize("empty", [False, True])
def test_global_latest_error_only_or_empty_run_does_not_reuse_stale_snapshot(shared_database, empty):
    factory, scopes = shared_database
    errors = (
        ()
        if empty
        else tuple(
            BrokerReadError(
                broker_id=scope[0],
                broker_name=environment,
                account_id=f"account-{environment}",
                code="UNAVAILABLE",
                message="Synthetic unavailable",
            )
            for environment, scope in scopes.items()
        )
    )
    with factory.begin() as session:
        repository = PortfolioSnapshotRepository(session)
        append_run(
            repository, NOW - timedelta(minutes=1), ((scopes["TEST"][0], "account-TEST", "RUB", "100", "100", "0"),)
        )
        append_run(repository, NOW, (), errors=errors)
    with factory() as session:
        for environment in ("TEST", "PROD"):
            repository = PortfolioSnapshotRepository(session, environment=environment)
            result = TradingSummaryService(repository).view()
            assert result.captured_at == NOW
            assert result.currencies == ()
            assert tuple(error.broker_id for error in result.errors) == (() if empty else (scopes[environment][0],))
            assert repository.run_for_bucket(NOW).errors == result.errors


@pytest.mark.parametrize("all_environments", [False, True])
def test_collector_composition_collects_one_global_bucket(shared_database, monkeypatch, all_environments):
    factory, scopes = shared_database
    called = []

    class SelectedAccountAdapter(PortfolioAdapter):
        def __init__(self, broker):
            super().__init__()
            self.account_id = broker.account_id

        async def list_accounts(self):
            return (BrokerAccount(self.account_id, "Synthetic", "OPEN", "BROKER"),)

    def create_adapter(_factory, broker):
        called.append(broker.id)
        return SelectedAccountAdapter(broker)

    monkeypatch.setattr("moex_sentinel.composition.PortfolioAdapterFactory.create", create_adapter)
    settings = Settings(
        _env_file=None, broker_access_mode="READ_ONLY", portfolio_snapshot_all_environments=all_environments
    )
    collector = build_portfolio_snapshot_collector(factory, factory.kw["bind"], settings, clock=lambda: NOW)
    result = asyncio.run(collector.execute())
    expected = {scope[0] for scope in scopes.values()} if all_environments else {scopes["TEST"][0]}
    assert set(called) == expected
    assert result.saved == len(expected)
    assert result.errors == ()
    second = asyncio.run(collector.execute())
    assert second.skipped is True
    assert second.run_id == result.run_id
    assert len(called) == len(expected)
    with factory() as session:
        repository = PortfolioSnapshotRepository(session)
        assert {snapshot.user_broker_id for snapshot in repository.latest_snapshots(result.run_id)} == expected


def test_collector_flag_from_environment_is_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS", raising=False)
    assert Settings(_env_file=None, broker_access_mode="READ_ONLY").portfolio_snapshot_all_environments is False
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS", "true")
    assert Settings(_env_file=None, broker_access_mode="READ_ONLY").portfolio_snapshot_all_environments is True


@pytest.mark.parametrize("state", ["DRAFT", "DISABLED", "ACTIVE"])
@pytest.mark.parametrize("environment", ["TEST", "PROD"])
def test_backend_rejects_foreign_put_and_delete(shared_database, state, environment):
    factory, scopes = shared_database
    foreign = "PROD" if environment == "TEST" else "TEST"
    foreign_id = scopes[foreign][0]
    with factory.begin() as session:
        broker = session.get(UserBrokerModel, foreign_id)
        broker.state = state
        if state == "DRAFT":
            broker.external_account_id = None
    settings = Settings(
        _env_file=None,
        broker_access_mode="READ_ONLY",
        application_environment=environment,
        auth_allowed_origin="https://example.test:8443",
        auth_session_cookie_name="__Host-prod-session",
    )
    application = build_application_usecases(factory, settings=settings)
    adapter = application.view_broker_settings.execute().adapters[0]
    draft = BrokerDraft(
        display_name="foreign overwrite",
        provider_code=adapter.provider_code,
        environment_code=adapter.environment_code,
        adapter_code=adapter.adapter_code,
        enabled=False,
        is_test=environment == "TEST",
        fields=tuple(BrokerField(field.name, field.default_value or "synthetic-token") for field in adapter.fields),
    )
    repository = UserBrokerRepository(factory)
    before = repository.get(foreign_id)
    with pytest.raises(UseCaseError) as put:
        application.save_broker_settings.execute(foreign_id, draft)
    assert put.value.code == "BROKER_NOT_FOUND"
    with pytest.raises(UseCaseError) as delete:
        application.delete_broker_settings.execute(foreign_id)
    assert delete.value.code == "BROKER_NOT_FOUND"
    assert repository.get(foreign_id) == before
