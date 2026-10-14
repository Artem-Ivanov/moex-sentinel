"""Benchmark adapters preserve the production admission context."""

from time import perf_counter
from uuid import UUID

import httpx
import pytest

from develop.benchmarks.core_http_concurrency import facts_for
from develop.benchmarks.pg_metrics import Profile
from develop.benchmarks.pg_server import NetworkDelivery, TimedIngress
from develop.benchmarks.synthetic import commands
from moex_sentinel.usecases.trading_fact_ingress import PublishTradingFactsUsecase
from sentinel_contracts.trading_facts import FactBatchResult
from trading_automaton.adapters.core_client import CoreClient
from trading_automaton.domain.errors import CoreOperationError


@pytest.mark.parametrize(("runtime_instance_id", "generation"), [(None, None), (UUID(int=42), 7)])
def test_timed_ingress_forwards_runtime_identity_unchanged(runtime_instance_id, generation):
    result = FactBatchResult(results=(), failures=())

    class Ingress:
        def publish(self, facts, **context):
            assert facts == []
            assert context == {"runtime_instance_id": runtime_instance_id, "generation": generation}
            return result

    profile = Profile()
    usecase = PublishTradingFactsUsecase(TimedIngress(Ingress(), profile))
    assert usecase.execute([], runtime_instance_id=runtime_instance_id, generation=generation) is result
    assert profile.samples["facts_per_request"] == [0]


@pytest.mark.parametrize(
    ("failure", "transport_errors", "retryable"), [("transport", 1, True), (503, 0, True), (403, 0, False)]
)
def test_network_delivery_tracks_only_wrapped_transport_failures(failure, transport_errors, retryable):
    facts = facts_for(commands(1))
    profile = Profile()
    profile.created[str(facts[0].event_id)] = perf_counter()
    delivery = NetworkDelivery("http://unused.invalid", profile)
    delivery.http.close()

    def response(request):
        if failure == "transport":
            raise httpx.ReadError("Synthetic lost acknowledgement", request=request)
        return httpx.Response(failure, json={"detail": "synthetic failure"})

    delivery.http = httpx.Client(base_url="http://unused.invalid", transport=httpx.MockTransport(response))
    delivery.client = CoreClient(delivery.http)
    try:
        with pytest.raises(CoreOperationError) as caught:
            delivery.publish_facts(facts)
        assert caught.value.retryable is retryable
        assert delivery.transport_errors == transport_errors
        assert delivery.replay_expected == (
            {str(facts[0].event_id): facts[0].model_dump(mode="json")} if transport_errors else {}
        )
    finally:
        delivery.close()
