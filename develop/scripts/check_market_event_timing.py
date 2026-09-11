"""Observe one 20-second Sandbox order-book stream; output aggregate timing only.

Run once from the repository root:
    .venv/bin/python -m develop.scripts.check_market_event_timing

Reads local Core configuration and active instrument identities through SELECT.
Uses normal SDK TLS and authorization, without orders, facts or database writes.
This independent connection measures its own delivery, not Core's existing stream.
"""

# ruff: noqa: PLC0415 - defer application imports until warnings/logs are silenced

import asyncio
import json
import logging
import math
import time
import warnings
from collections import Counter
from datetime import UTC, datetime

DURATION_SECONDS = 20


def summary(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 2),
        "p50_ms": round(ordered[math.ceil(len(ordered) * 0.50) - 1], 2),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
        "max_ms": round(ordered[-1], 2),
        "over_2000ms_count": sum(value > 2000 for value in ordered),
        "negative_count": sum(value < 0 for value in ordered),
    }


async def observe(connection, instruments):
    from grpc import StatusCode
    from t_tech.invest.async_services import AsyncServices
    from t_tech.invest.channels import create_channel
    from t_tech.invest.grpc import marketdata_pb2

    ids = set(instruments)
    counts = Counter()
    last_arrival = {}
    last_formed = {}
    last_payload = {}
    arrival_ages, arrival_gaps, formation_gaps, ping_ages = [], [], [], []
    initial_ages, update_ages = [], []
    unchanged = out_of_order = unexpected = inconsistent = messages = 0
    accepted_subscriptions = set()
    rejected_subscriptions = set()
    request = marketdata_pb2.MarketDataRequest(
        subscribe_order_book_request=marketdata_pb2.SubscribeOrderBookRequest(
            subscription_action=1,
            instruments=[marketdata_pb2.OrderBookInstrument(instrument_id=item, depth=20) for item in instruments],
        )
    )
    hold = asyncio.Event()
    finished = asyncio.Event()
    sent = False

    async def requests():
        nonlocal sent
        try:
            sent = True
            yield request
            await hold.wait()
        finally:
            finished.set()

    started = time.monotonic()
    result = "WINDOW_COMPLETE"
    grpc_status = None
    async with create_channel(target=connection.target, force_async=True) as channel:
        service = AsyncServices(channel, token=connection.token).market_data_stream
        iterator = requests()
        call = service.stub.MarketDataStream(iterator, metadata=service.metadata, timeout=DURATION_SECONDS)
        try:
            async with asyncio.timeout(DURATION_SECONDS):
                async for response in call:
                    received = time.monotonic()
                    now = datetime.now(UTC)
                    messages += 1
                    if response.HasField("subscribe_order_book_response"):
                        for item in response.subscribe_order_book_response.order_book_subscriptions:
                            if item.instrument_uid in ids:
                                target = (
                                    accepted_subscriptions if item.subscription_status == 1 else rejected_subscriptions
                                )
                                target.add(item.instrument_uid)
                    if response.HasField("ping"):
                        ping_ages.append((now - response.ping.time.ToDatetime(tzinfo=UTC)).total_seconds() * 1000)
                    if not response.HasField("orderbook"):
                        continue
                    book = response.orderbook
                    key = book.instrument_uid
                    if key not in ids:
                        unexpected += 1
                        continue
                    formed = book.time.ToDatetime(tzinfo=UTC)
                    age = (now - formed).total_seconds() * 1000
                    arrival_ages.append(age)
                    # An initial subscription snapshot may legitimately predate
                    # this connection; keep it separate from later updates.
                    (update_ages if key in last_arrival else initial_ages).append(age)
                    counts[key] += 1
                    inconsistent += not book.is_consistent
                    # Compare market values only in memory; no identities/prices
                    # or payload hashes are emitted or persisted.
                    payload = (
                        book.depth,
                        book.is_consistent,
                        book.order_book_type,
                        tuple((item.price.units, item.price.nano, item.quantity) for item in book.bids),
                        tuple((item.price.units, item.price.nano, item.quantity) for item in book.asks),
                    )
                    if key in last_arrival:
                        arrival_gaps.append((received - last_arrival[key]) * 1000)
                        formation_gaps.append((formed - last_formed[key]).total_seconds() * 1000)
                        unchanged += payload == last_payload[key]
                        out_of_order += formed < last_formed[key]
                    last_arrival[key], last_formed[key], last_payload[key] = received, formed, payload
                result = "STREAM_ENDED"
        except TimeoutError:
            pass
        except Exception as error:
            code = error.code() if callable(getattr(error, "code", None)) else None
            grpc_status = code.name if isinstance(code, StatusCode) else "LOCAL_ERROR"
            if code != StatusCode.DEADLINE_EXCEEDED or time.monotonic() - started < DURATION_SECONDS - 0.5:
                result = "RPC_FAILED"
        finally:
            call.cancel()
            hold.set()
            if sent:
                try:
                    await asyncio.wait_for(finished.wait(), 1)
                except TimeoutError:
                    result = "CLEANUP_TIMEOUT"
            await iterator.aclose()
    ended = time.monotonic()
    return {
        "result": result,
        "grpc_status": grpc_status,
        "observed_seconds": round(ended - started, 2),
        "instrument_count": len(ids),
        "subscription_accepted_count": len(accepted_subscriptions),
        "subscription_rejected_count": len(rejected_subscriptions),
        "message_count": messages,
        "book_count": sum(counts.values()),
        "instruments_with_books": len(counts),
        "per_instrument_book_count_min": min((counts[key] for key in ids), default=0),
        "per_instrument_book_count_max": max(counts.values(), default=0),
        "arrival_age": summary(arrival_ages),
        "initial_book_age": summary(initial_ages),
        "subsequent_book_age": summary(update_ages),
        "interarrival_gap": summary(arrival_gaps),
        "formation_gap": summary(formation_gaps),
        "ping_age": summary(ping_ages),
        "unchanged_payload_count": unchanged,
        "out_of_order_count": out_of_order,
        "inconsistent_count": inconsistent,
        "unexpected_instrument_count": unexpected,
        "terminal_silence": summary([(ended - last_arrival.get(key, started)) * 1000 for key in ids]),
    }


def main():
    warnings.simplefilter("ignore", DeprecationWarning)
    logging.disable(logging.CRITICAL)
    from dotenv import dotenv_values
    from sqlalchemy import URL, create_engine, text
    from sqlalchemy.orm import sessionmaker

    from moex_sentinel.services.automaton_brokers import AutomatonBrokerService
    from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository

    values = dotenv_values(".env")
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=values["POSTGRES_USER"],
            password=values["POSTGRES_PASSWORD"],
            host="127.0.0.1",
            port=int(values.get("POSTGRES_PORT") or "55432"),
            database=values["POSTGRES_DB"],
        )
    )
    try:
        with engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT a.user_broker_id, i.external_instrument_id FROM trading_automations a "
                    "JOIN broker_instruments i ON i.id=a.instrument_id AND i.user_broker_id=a.user_broker_id "
                    "WHERE a.state='IN_WORK'"
                )
            ).all()
        if not rows:
            return {"result": "NO_ACTIVE_SOURCE"}
        sources = {row[0] for row in rows}
        instruments = sorted({row[1] for row in rows})
        if len(sources) != 1 or len(instruments) != 6:
            return {
                "result": "EXPECTED_SIX_INSTRUMENTS_ONE_SOURCE",
                "source_count": len(sources),
                "instrument_count": len(instruments),
            }
        connection = AutomatonBrokerService(UserBrokerRepository(sessionmaker(engine))).connection(rows[0][0])
        return asyncio.run(observe(connection, instruments))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        output = main()
    except Exception:
        output = {"result": "LOCAL_ERROR"}
    print(json.dumps(output))  # noqa: T201 - fixed diagnostic counters only
