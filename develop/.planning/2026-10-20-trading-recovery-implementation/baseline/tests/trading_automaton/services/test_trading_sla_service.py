from datetime import UTC, datetime, timedelta

from trading_automaton.services.trading_sla import TradingSlaService

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def test_classifies_dispatch_latency_boundaries() -> None:
    service = TradingSlaService()

    assert service.classify(NOW, NOW + timedelta(milliseconds=100)).code == "OK"
    assert service.classify(NOW, NOW + timedelta(milliseconds=101)).code == "DEGRADED"
    assert service.classify(NOW, NOW + timedelta(milliseconds=251)).code == "WARNING"
    assert service.classify(NOW, NOW + timedelta(milliseconds=501)).code == "BREACHED"
    assert service.classify(NOW, NOW + timedelta(milliseconds=1001)).code == ("STALE_DECISION_DROPPED")
