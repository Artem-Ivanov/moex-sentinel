"""Classification of snapshot-to-SDK-dispatch latency."""

from datetime import datetime

from trading_automaton.domain.dtos import SlaResult


class TradingSlaService:
    def classify(self, snapshot_at: datetime, dispatched_at: datetime) -> SlaResult:
        latency_ms = max(0.0, (dispatched_at - snapshot_at).total_seconds() * 1000)
        if latency_ms <= 100:
            code = "OK"
        elif latency_ms <= 250:
            code = "DEGRADED"
        elif latency_ms <= 500:
            code = "WARNING"
        elif latency_ms <= 1000:
            code = "BREACHED"
        else:
            code = "STALE_DECISION_DROPPED"
        return SlaResult(code, latency_ms)
