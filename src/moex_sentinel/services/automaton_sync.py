"""Stateless heartbeat boundary for the independent Worker."""

from datetime import datetime

from moex_sentinel.domain.repository_records import HeartbeatRecord


class AutomatonSyncService:
    def record_heartbeat(self, worker_id: str, occurred_at: datetime) -> HeartbeatRecord:
        return HeartbeatRecord(worker_id=worker_id, occurred_at=occurred_at)
