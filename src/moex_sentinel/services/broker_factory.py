"""Construction of SDK-neutral portfolio adapters from broker settings."""

from collections.abc import Callable

from moex_sentinel.domain.brokers import Broker
from moex_sentinel.domain.user_brokers import UserBroker, UserBrokerState
from moex_sentinel.services.market_data_ports import MarketDataPort
from moex_sentinel.services.portfolio_ports import PortfolioPort

TINVEST_SANDBOX_ADAPTER = "TINVEST_SANDBOX"
TINVEST_SANDBOX_TARGET = "sandbox-invest-public-api.tbank.ru:443"


class PortfolioAdapterFactory:
    def __init__(self, adapter_builder: Callable[[str, str], PortfolioPort]) -> None:
        self._adapter_builder = adapter_builder

    def create(self, broker: Broker) -> PortfolioPort:
        # Account discovery is required before a draft can become ACTIVE.
        draft = isinstance(broker, UserBroker) and broker.state is UserBrokerState.DRAFT
        if not broker.enabled and not draft:
            raise ValueError("Подключение брокера отключено.")
        if broker.adapter_code != TINVEST_SANDBOX_ADAPTER:
            raise ValueError("Адаптер брокера не поддерживается.")

        fields = {field.name: field.value.strip() for field in broker.fields}
        token = fields.get("token", "")
        target = fields.get("fqdn", "")
        if not token or not target:
            raise ValueError("Не заполнены обязательные настройки подключения.")  # noqa: RUF001
        if target != TINVEST_SANDBOX_TARGET:
            raise ValueError("Для MVP разрешён только адрес песочницы T-Invest.")
        return self._adapter_builder(token, target)


class MarketDataAdapterFactory:
    def __init__(self, adapter_builder: Callable[[str, str], MarketDataPort]) -> None:
        self._adapter_builder = adapter_builder

    def create(self, broker: Broker) -> MarketDataPort:
        if not broker.enabled:
            raise ValueError("Подключение брокера отключено.")
        if broker.adapter_code != TINVEST_SANDBOX_ADAPTER:
            raise ValueError("Адаптер брокера не поддерживается.")
        fields = {field.name: field.value.strip() for field in broker.fields}
        token = fields.get("token", "")
        target = fields.get("fqdn", "")
        if not token or target != TINVEST_SANDBOX_TARGET:
            raise ValueError("Настройки тестового подключения некорректны.")
        return self._adapter_builder(token, target)
