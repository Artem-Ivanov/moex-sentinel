from datetime import UTC, datetime, timedelta

from trading_automaton.services.broker_rate_limit import BrokerRateLimitService

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def test_token_bucket_never_waits_and_refills_over_time() -> None:
    limiter = BrokerRateLimitService(capacity=2, refill_per_second=1)

    assert limiter.try_acquire(NOW)
    assert limiter.try_acquire(NOW)
    assert not limiter.try_acquire(NOW)
    assert limiter.try_acquire(NOW + timedelta(seconds=1))
    assert not limiter.try_acquire(NOW + timedelta(seconds=1))
