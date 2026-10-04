"""Contour and access safety at the broker boundary."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from moex_sentinel.adapters.tinvest.order_execution import TInvestOrderExecutionAdapter
from moex_sentinel.adapters.tinvest.portfolio import TInvestPortfolioAdapter
from moex_sentinel.config import Settings
from moex_sentinel.services.automations import AutomationService
from moex_sentinel.services.broker_factory import PortfolioAdapterFactory
from moex_sentinel.services.environment import PinnedEnvironment
from sentinel_contracts.broker_execution import BrokerConnection, OrderSide
from sentinel_contracts.tinvest import resolve_tinvest_endpoint
from tests.adapters.test_tinvest_order_execution_adapter import money, quotation
from tests.services.test_position_adoption_service import Broker, Repository, adopt, instrument, position

PROD = "invest-public-api.tbank.ru:443"


@pytest.mark.asyncio
async def test_prod_accounts_use_users_service():
    class Client:
        async def __aenter__(self):
            return SimpleNamespace(users=SimpleNamespace(get_accounts=self.accounts))

        async def __aexit__(self, *args):
            pass

        async def accounts(self):
            return SimpleNamespace(accounts=[])

    adapter = TInvestPortfolioAdapter("synthetic", PROD, client_factory=lambda *a, **k: Client())
    assert await adapter.list_accounts() == ()


@pytest.mark.asyncio
async def test_read_only_rejects_post_and_cancel_before_client_creation():
    def client(*args, **kwargs):
        pytest.fail("READ_ONLY reached SDK")

    adapter = TInvestOrderExecutionAdapter("synthetic", PROD, client_factory=client, access_mode="READ_ONLY")
    with pytest.raises(ValueError, match="READ_ONLY"):
        await adapter.submit_limit_order("account", "uid", OrderSide.BUY, 1, Decimal(1), "key")
    with pytest.raises(ValueError, match="READ_ONLY"):
        await adapter.cancel_order("account", "order")


def test_connection_rejects_environment_is_test_mismatch():
    with pytest.raises(ValueError, match="environment|adapter|target|broker_access_mode|PROD|pinned|inactive"):
        BrokerConnection("broker", "TINVEST_PROD", PROD, "synthetic", True, environment="PROD")


def test_resolver_rejects_mismatched_adapter_target():
    assert resolve_tinvest_endpoint("PROD", "t_invest", PROD) == PROD
    for environment, adapter, target in [
        ("PROD", "TINVEST_SANDBOX", PROD),
        ("TEST", "t_invest", PROD),
        ("PROD", "t_invest", "foreign"),
    ]:
        with pytest.raises(ValueError, match="environment|adapter|target|broker_access_mode|PROD|pinned|inactive"):
            resolve_tinvest_endpoint(environment, adapter, target)


def test_core_settings_requires_mode_and_rejects_prod_trade(monkeypatch):
    monkeypatch.delenv("BROKER_ACCESS_MODE", raising=False)
    with pytest.raises(ValueError, match="environment|adapter|target|broker_access_mode|PROD|pinned|inactive"):
        Settings(_env_file=None)
    with pytest.raises(ValueError, match="environment|adapter|target|broker_access_mode|PROD|pinned|inactive"):
        Settings(_env_file=None, application_environment="PROD", broker_access_mode="TRADE")


def test_prod_factory_routes_without_sandbox_adapter():
    broker = SimpleNamespace(
        enabled=True,
        adapter_code="TINVEST_PROD",
        is_test=False,
        fields=[SimpleNamespace(name="token", value="synthetic"), SimpleNamespace(name="fqdn", value=PROD)],
    )
    assert PortfolioAdapterFactory(lambda token, target: target).create(broker) == PROD


def test_pinned_environment_rejects_switch():
    pin = PinnedEnvironment("PROD")
    assert pin.view().active_environment == "PROD"
    with pytest.raises(ValueError, match="environment|adapter|target|broker_access_mode|PROD|pinned|inactive"):
        pin.switch("TEST")


def test_core_read_only_rejects_direct_trading_commands():
    service = AutomationService(SimpleNamespace(), access_mode="READ_ONLY")
    for call in [
        lambda: service.create(broker_id="b", account_id="a", instrument_id="u"),
        lambda: service.resume("automation"),
        lambda: service.close("automation"),
    ]:
        with pytest.raises(ValueError, match="READ_ONLY"):
            call()


@pytest.mark.asyncio
async def test_prod_portfolio_subtracts_blocked_cash():
    class Client:
        async def __aenter__(self):
            return SimpleNamespace(
                operations=SimpleNamespace(get_portfolio=self.portfolio, get_positions=self.positions)
            )

        async def __aexit__(self, *args):
            pass

        async def portfolio(self, **kwargs):
            return SimpleNamespace(total_amount_portfolio=money("200"), expected_yield=quotation("0"))

        async def positions(self, **kwargs):
            return SimpleNamespace(money=[money("100")], blocked=[money("25")])

    portfolio = await TInvestPortfolioAdapter("synthetic", PROD, lambda *a, **k: Client()).get_portfolio("account")
    assert portfolio.free_cash.amount == Decimal(75)


@pytest.mark.asyncio
async def test_prod_stop_order_blocks_adoption_even_without_regular_order():
    class Client:
        async def __aenter__(self):
            return SimpleNamespace(
                orders=SimpleNamespace(get_orders=self.orders), stop_orders=SimpleNamespace(get_stop_orders=self.stops)
            )

        async def __aexit__(self, *args):
            pass

        async def orders(self, **kwargs):
            return SimpleNamespace(orders=[])

        async def stops(self, **kwargs):
            return SimpleNamespace(stop_orders=[SimpleNamespace(instrument_uid="uid", stop_order_id="stop")])

    adapter = TInvestOrderExecutionAdapter("synthetic", PROD, lambda *a, **k: Client())
    assert len(await adapter.list_active_orders("account", "uid")) == 1


def test_blocked_inventory_is_not_adopted():
    repo = Repository(instrument())
    value = position().model_copy(update={"blocked": True})
    result = adopt(Broker((value,)), repo)
    assert result.held == 1
    assert repo.candidates == []


@pytest.mark.asyncio
async def test_selected_account_rejects_foreign_read_before_sdk():
    def client(*args, **kwargs):
        pytest.fail("Foreign account reached SDK")

    adapter = TInvestOrderExecutionAdapter("synthetic", PROD, client, account_id="selected")
    with pytest.raises(ValueError, match="account"):
        await adapter.get_order_state("foreign", "order")


def test_automation_reads_filter_inactive_contour():
    records = [SimpleNamespace(broker_id="test"), SimpleNamespace(broker_id="prod")]
    repo = SimpleNamespace(list_active=lambda: records, get=lambda key: records[0])
    brokers = SimpleNamespace(
        get=lambda key: SimpleNamespace(id=key, is_test=key == "test"),
        list=lambda: [SimpleNamespace(id=key, is_test=key == "test") for key in ("test", "prod")],
    )
    service = AutomationService(repo, environment=PinnedEnvironment("PROD"), brokers=brokers)
    assert service.list_active() == [records[1]]
    with pytest.raises(ValueError, match="environment|adapter|target|broker_access_mode|PROD|pinned|inactive"):
        service.get("testautomation")


def test_default_automation_service_is_read_only():
    service = AutomationService(SimpleNamespace())
    with pytest.raises(ValueError, match="READ_ONLY"):
        service.create(broker_id="b", account_id="a", instrument_id="i")


@pytest.mark.asyncio
async def test_prod_blocked_lots_prevent_adoption_without_blocked_boolean():
    class Client:
        async def __aenter__(self):
            return SimpleNamespace(
                operations=SimpleNamespace(get_portfolio=self.portfolio, get_positions=self.positions)
            )

        async def __aexit__(self, *args):
            pass

        async def portfolio(self, **kwargs):
            return SimpleNamespace(
                positions=[
                    SimpleNamespace(
                        instrument_uid="uid",
                        ticker="TEST",
                        quantity_lots=quotation("1"),
                        average_position_price=money("100"),
                        current_price=money("100"),
                        expected_yield=quotation("0"),
                        blocked=False,
                        blocked_lots=quotation("1"),
                    )
                ]
            )

        async def positions(self, **kwargs):
            return SimpleNamespace(securities=[])

    positions = await TInvestPortfolioAdapter("synthetic", PROD, lambda *a, **k: Client()).get_positions("account")
    assert positions[0].blocked is True


def test_trade_create_rejects_foreign_contour_before_repository_write():
    def persist(**kwargs):
        pytest.fail("Foreign contour was persisted")

    service = AutomationService(
        SimpleNamespace(create=persist),
        access_mode="TRADE",
        environment=PinnedEnvironment("TEST"),
        brokers=SimpleNamespace(get=lambda key: SimpleNamespace(is_test=False)),
    )
    with pytest.raises(ValueError, match="environment"):
        service.create(broker_id="prod", account_id="a", instrument_id="i")
