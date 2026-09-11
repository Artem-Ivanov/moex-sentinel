"""Read-only Analytics probe using the running Worker's cached market identity.

Run inside the existing Worker container, without installing or starting anything:
    docker compose exec -T trading-automaton python - --require-all-fresh \
        < develop/scripts/check_analytics_runtime.py

SQLite is opened with mode=ro. Only Analytics snapshots are requested; no broker,
execution, fact publication or database mutation API is called. JSON output never
includes instrument/source/account identifiers, prices, credentials or raw errors.
"""

# Keep application imports lazy so --help needs only the standard library.
# ruff: noqa: PLC0415

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
import warnings
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from urllib.parse import quote


def emit(values: dict[str, object]) -> None:
    print(json.dumps(values), flush=True)  # noqa: T201 - this CLI emits safe JSON counters


def worker_progress(path: str) -> dict[str, object]:
    """Read aggregate durable progress across this Worker, without domain values."""
    with closing(sqlite3.connect("file:" + quote(path) + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        count, last_at = db.execute("SELECT COUNT(*), MAX(occurred_at) FROM trade_decisions").fetchone()
        intents = dict(db.execute("SELECT state, COUNT(*) FROM broker_intents GROUP BY state").fetchall())
        outbox = dict(db.execute("SELECT delivery_state, COUNT(*) FROM fact_outbox GROUP BY delivery_state").fetchall())
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "trade_decision_count": count,
        "last_trade_decision_at": last_at,
        "broker_intent_state_counts": intents,
        "fact_outbox_state_counts": outbox,
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--attempts", type=int, default=4, help="Number of snapshot checks (1–120).")
    parser.add_argument("--interval", type=float, default=0.5, help="Seconds between checks (0–10).")
    parser.add_argument("--timeout", type=float, default=1.0, help="Analytics HTTP timeout in seconds (0.1–30).")
    parser.add_argument(
        "--require-all-fresh", action="store_true", help="Exit 1 unless the final check accepts every instrument."
    )
    args = parser.parse_args()
    if not 1 <= args.attempts <= 120 or not 0 <= args.interval <= 10 or not 0.1 <= args.timeout <= 30:
        parser.error("Numeric options are outside the supported bounds.")
    return args


async def probe(args: argparse.Namespace) -> bool:
    # Container dependencies are imported after argument parsing so --help also
    # works on a host without the application environment.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import httpx
        from sqlalchemy.engine import make_url

        from sentinel_contracts.analytics import AdaptiveThresholds, AnalyticsSnapshotRequest
        from sentinel_contracts.broker_execution import OrderBookSnapshot
        from trading_automaton.adapters.analytics_client import AnalyticsClient
        from trading_automaton.config import AutomatonSettings, StrategySettings
        from trading_automaton.services.analytics_runtime import AnalyticsBrokerRuntime
        from trading_automaton.services.order_book_validation import OrderBookValidationService

    settings = AutomatonSettings()
    strategy = StrategySettings()
    path = make_url(settings.database_url).database
    if not path or path == ":memory:":
        raise ValueError("A persistent Worker SQLite database is required.")
    with closing(sqlite3.connect("file:" + quote(path) + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT broker_id, user_broker_id, instrument_id FROM cached_automations WHERE state='IN_WORK'"
        ).fetchall()
    groups: dict[str, set[str]] = defaultdict(set)
    for broker_id, _, instrument_id in rows:
        if not broker_id or not instrument_id:
            raise ValueError("Cached market identity is incomplete.")
        groups[broker_id].add(instrument_id)
    instrument_count = sum(len(ids) for ids in groups.values())
    before = worker_progress(path)
    emit(
        {
            "cached_in_work_count": len(rows),
            "source_count": len(groups),
            "instrument_count": instrument_count,
            "broker_id_equals_user_broker_id_count": sum(broker == user_broker for broker, user_broker, _ in rows),
            "worker_before": before,
        }
    )

    class FreshnessClock:
        """Supply only the clock used by Worker's pure snapshot acceptance method."""

        @staticmethod
        def _now() -> datetime:
            return datetime.now(UTC)

    previous: dict[tuple[str, str], object] = {}
    same_id_comparisons = 0
    coherence_errors = 0
    all_fresh = False
    async with httpx.AsyncClient(base_url=settings.analytics_url, timeout=args.timeout, trust_env=False) as http:
        client = AnalyticsClient(http)
        for attempt in range(args.attempts):
            counts: Counter[str] = Counter()
            reasons: Counter[str] = Counter()
            accepted_count = profiles_ok = 0
            candle_counts: list[int] = []
            book_ages: list[float] = []
            envelope_ages: list[float] = []
            latencies: list[float] = []
            for source_id, ids in groups.items():
                started = time.monotonic()
                try:
                    request = AnalyticsSnapshotRequest(
                        source_id=source_id,
                        instrument_ids=tuple(sorted(ids)),
                        fallback=AdaptiveThresholds(
                            strategy.averaging_step_percent, strategy.partial_take_profit_percent, "STRATEGY"
                        ),
                    )
                    frame = await client.snapshot(request)
                    AnalyticsBrokerRuntime._validate_response(frame, request)
                except httpx.HTTPStatusError as error:
                    reasons[f"HTTP_{error.response.status_code}"] += 1
                    continue
                except (httpx.HTTPError, ValueError) as error:
                    reasons[type(error).__name__] += 1
                    continue
                finally:
                    latencies.append((time.monotonic() - started) * 1000)
                profiles_ok += 1
                # Call the same pure gate as production, without constructing a
                # broker runtime, execution adapter or any writable repository.
                accepted = AnalyticsBrokerRuntime._fresh_instruments(FreshnessClock(), frame)
                accepted_count += len(accepted)
                key = (frame.snapshot_id, frame.profile_id)
                stable = {
                    "captured_at": frame.captured_at,
                    "ttl_ms": frame.ttl_ms,
                    "instruments": tuple(item.model_dump(exclude={"freshness"}) for item in frame.instruments),
                }
                if key in previous:
                    same_id_comparisons += 1
                    coherence_errors += previous[key] != stable
                previous[key] = stable
                now = datetime.now(UTC)
                ttl = timedelta(milliseconds=min(frame.ttl_ms, 2000))
                envelope_age = now - frame.captured_at
                envelope_ages.append(envelope_age.total_seconds() * 1000)
                if not timedelta() <= envelope_age <= ttl:
                    reasons["SNAPSHOT_OUTSIDE_TTL"] += 1
                for item in frame.instruments:
                    counts[item.freshness] += 1
                    candle_counts.append(len(item.candles))
                    book = item.market.order_book
                    status = item.market.trading_status
                    if not item.available:
                        reasons["SOURCE_UNAVAILABLE"] += 1
                    if status is None or not status.limit_order_available or not status.api_trade_available:
                        reasons["TRADING_STATUS_UNAVAILABLE"] += 1
                    if book is None:
                        reasons["ORDER_BOOK_UNAVAILABLE"] += 1
                        continue
                    book_ages.append((now - book.captured_at).total_seconds() * 1000)
                    if not book.is_consistent:
                        reasons["INCONSISTENT_ORDER_BOOK"] += 1
                    checked = OrderBookValidationService().validate(
                        OrderBookSnapshot(book.bids, book.asks, book.captured_at), now=now, max_age=ttl
                    )
                    if not checked.valid:
                        reasons[checked.reason_code or "INVALID_ORDER_BOOK"] += 1
            all_fresh = instrument_count > 0 and accepted_count == instrument_count and not coherence_errors
            emit(
                {
                    "attempt": attempt + 1,
                    "instrument_count": instrument_count,
                    "analytics_fresh": counts["FRESH"],
                    "analytics_stale": counts["STALE"],
                    "analytics_unavailable": counts["UNAVAILABLE"],
                    "worker_fresh_gate_pass_count": accepted_count,
                    "profile_and_instrument_set_match_count": profiles_ok,
                    "candle_count_min": min(candle_counts, default=0),
                    "candle_count_max": max(candle_counts, default=0),
                    "max_book_age_ms": round(max(book_ages), 2) if book_ages else None,
                    "max_envelope_age_ms": round(max(envelope_ages), 2) if envelope_ages else None,
                    "max_latency_ms": round(max(latencies, default=0), 2),
                    "same_id_comparisons": same_id_comparisons,
                    "same_id_coherence_errors": coherence_errors,
                    "reason_code_counts": dict(reasons),
                    "all_fresh": all_fresh,
                }
            )
            if attempt + 1 < args.attempts:
                await asyncio.sleep(args.interval)
    after = worker_progress(path)
    emit(
        {
            "worker_after": after,
            "trade_decision_count_delta": int(after["trade_decision_count"]) - int(before["trade_decision_count"]),
            "last_trade_decision_advanced": after["last_trade_decision_at"] != before["last_trade_decision_at"],
        }
    )
    return all_fresh


def main() -> int:
    args = arguments()
    try:
        all_fresh = asyncio.run(probe(args))
    except Exception as error:
        # Exception messages and tracebacks may embed connection strings or
        # validation input, so diagnostics expose the exception type only.
        emit({"error_type": type(error).__name__})
        return 2
    return 0 if all_fresh or not args.require_all_fresh else 1


if __name__ == "__main__":
    raise SystemExit(main())
