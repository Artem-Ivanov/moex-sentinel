"""Non-blocking token bucket for broker order dispatch capacity."""

from datetime import datetime


class BrokerRateLimitService:
    def __init__(self, *, capacity: int, refill_per_second: float) -> None:
        if capacity <= 0 or refill_per_second <= 0:
            raise ValueError("Rate limit values must be positive.")
        self._capacity = float(capacity)
        self._refill_per_second = refill_per_second
        self._tokens = float(capacity)
        self._updated_at: datetime | None = None

    def try_acquire(self, now: datetime) -> bool:
        if self._updated_at is not None:
            elapsed = max(0.0, (now - self._updated_at).total_seconds())
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._refill_per_second,
            )
        self._updated_at = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True
