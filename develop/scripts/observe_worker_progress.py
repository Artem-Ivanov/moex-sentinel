"""Sample aggregate Worker progress from SQLite opened in read-only mode.

docker compose exec -T trading-automaton python - --samples 120 --interval 5 \
    < develop/scripts/observe_worker_progress.py

No identifiers, payloads, prices or credentials are emitted. Retry counts cover
currently retained outbox rows, not HTTP retries inside a client or ACKed rows.
"""

import argparse
import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def snapshot(path: Path, now: datetime) -> dict[str, object]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("BEGIN")
        count, last_at = db.execute("SELECT COUNT(*), MAX(occurred_at) FROM trade_decisions").fetchone()
        states = dict(db.execute("SELECT delivery_state, COUNT(*) FROM fact_outbox GROUP BY delivery_state"))
        oldest, retries = db.execute(
            "SELECT MIN(CASE WHEN delivery_state='PENDING' THEN created_at END), "
            "COALESCE(MAX(retry_count), 0) FROM fact_outbox"
        ).fetchone()

    def age(value: str | None) -> float | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)  # Worker UTCDateTime stores naive UTC in SQLite.
        return round((now - parsed).total_seconds() * 1000, 3)

    return {
        "observed_at": now.isoformat(),
        "trade_decision_count": count,
        "decision_age_ms": age(last_at),
        "outbox_state_counts": states,
        "oldest_pending_age_ms": age(oldest),
        "outbox_retry_count_max": retries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("/app/data/trading_automaton.db"))
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.samples <= 720 or not 0.1 <= args.interval <= 60:
        parser.error("samples must be 1–720; interval must be 0.1–60 seconds")
    for index in range(args.samples):
        try:
            result = snapshot(args.database, datetime.now(UTC))
        except (sqlite3.Error, ValueError, OSError) as error:
            print(json.dumps({"error_type": type(error).__name__}), flush=True)  # noqa: T201
            return 1
        print(json.dumps(result), flush=True)  # noqa: T201
        if index + 1 < args.samples:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
