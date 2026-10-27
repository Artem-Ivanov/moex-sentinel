"""Batch scope filtering keeps historical scopes and broker/environment errors."""

from types import SimpleNamespace

import pytest

from moex_sentinel.domain.user_brokers import UserBrokerNotFoundError
from moex_sentinel.services.automations import AutomationService
from moex_sentinel.services.environment import PinnedEnvironment


class Brokers:
    def __init__(self, values):
        self.values = values
        self.list_calls = 0
        self.get_calls = []

    def list(self):
        self.list_calls += 1
        return list(self.values.values())

    def get(self, key):
        self.get_calls.append(key)
        if key not in self.values:
            raise UserBrokerNotFoundError(key)
        return self.values[key]


class Environment:
    def __init__(self, active="TEST", error=None):
        self.active = active
        self.error = error
        self.calls = 0

    def view(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return PinnedEnvironment(self.active).view()


def service_for(records, *, brokers, environment):
    repository = SimpleNamespace(get_many=lambda ids: records, list_active=lambda: records)
    return AutomationService(repository, brokers=brokers, environment=environment)


def invoke(service, method):
    return service.get_many(["requested"]) if method == "get_many" else service.list_active()


@pytest.mark.parametrize(
    ("method", "count"), [(method, count) for method in ("get_many", "list_active") for count in (1, 50)]
)
def test_one_broker_batch_and_one_environment_snapshot(method, count):
    brokers = Brokers({str(index): SimpleNamespace(id=str(index), is_test=index % 2 == 0) for index in range(count)})
    records = [SimpleNamespace(id=str(index), broker_id=str(index)) for index in range(count)]
    environment = Environment()
    service = service_for(records, brokers=brokers, environment=environment)
    assert invoke(service, method) == records[::2]
    assert brokers.list_calls == 1
    assert brokers.get_calls == []
    assert environment.calls == 1


@pytest.mark.parametrize("method", ["get_many", "list_active"])
def test_empty_result_skips_scope_reads(method):
    brokers = Brokers({})
    environment = Environment(error=RuntimeError("must not read environment"))
    assert invoke(service_for([], brokers=brokers, environment=environment), method) == []
    assert brokers.list_calls == 0
    assert brokers.get_calls == []
    assert environment.calls == 0


@pytest.mark.parametrize(
    ("method", "missing_dependency"),
    [(method, missing) for method in ("get_many", "list_active") for missing in ("brokers", "environment")],
)
def test_unconfigured_scope_dependencies_preserve_bypass(method, missing_dependency):
    brokers = Brokers({})
    environment = Environment(error=RuntimeError("must not read environment"))
    records = [SimpleNamespace(broker_id="missing")]
    service = service_for(
        records,
        brokers=None if missing_dependency == "brokers" else brokers,
        environment=None if missing_dependency == "environment" else environment,
    )
    assert invoke(service, method) == records
    assert brokers.list_calls == 0
    assert brokers.get_calls == []
    assert environment.calls == 0


@pytest.mark.parametrize("method", ["get_many", "list_active"])
def test_missing_first_broker_error_precedes_environment_failure(method):
    brokers = Brokers({})
    environment = Environment(error=RuntimeError("environment failed"))
    records = [SimpleNamespace(broker_id="missing")]
    with pytest.raises(UserBrokerNotFoundError) as caught:
        invoke(service_for(records, brokers=brokers, environment=environment), method)
    assert caught.value.args == ("missing",)
    assert environment.calls == 0


@pytest.mark.parametrize("method", ["get_many", "list_active"])
def test_first_environment_error_precedes_missing_later_broker(method):
    brokers = Brokers({"first": SimpleNamespace(id="first", is_test=True)})
    environment = Environment(error=RuntimeError("environment failed"))
    records = [SimpleNamespace(broker_id="first"), SimpleNamespace(broker_id="missing")]
    with pytest.raises(RuntimeError, match="environment failed"):
        invoke(service_for(records, brokers=brokers, environment=environment), method)
    assert "missing" not in brokers.get_calls
    assert environment.calls == 1


@pytest.mark.parametrize("method", ["get_many", "list_active"])
def test_missing_later_broker_is_not_silently_filtered(method):
    brokers = Brokers({"first": SimpleNamespace(id="first", is_test=False)})
    environment = Environment()
    records = [SimpleNamespace(broker_id="first"), SimpleNamespace(broker_id="missing")]
    with pytest.raises(UserBrokerNotFoundError) as caught:
        invoke(service_for(records, brokers=brokers, environment=environment), method)
    assert caught.value.args == ("missing",)
    assert environment.calls == 1


@pytest.mark.parametrize(
    ("method", "active"), [(method, active) for method in ("get_many", "list_active") for active in ("TEST", "PROD")]
)
def test_historical_archived_broker_scopes_are_filtered_only_by_environment(method, active):
    brokers = Brokers(
        {
            "test": SimpleNamespace(id="test", is_test=True, archived_at="historical", enabled=False),
            "prod": SimpleNamespace(id="prod", is_test=False, archived_at="historical", enabled=False),
        }
    )
    records = [SimpleNamespace(broker_id="test"), SimpleNamespace(broker_id="prod")]
    expected = [records[0 if active == "TEST" else 1]]
    assert invoke(service_for(records, brokers=brokers, environment=Environment(active)), method) == expected
