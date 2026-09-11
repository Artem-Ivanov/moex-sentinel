"""Bounded read-only configured market transport probe; emits only diagnostic codes."""

# ruff: noqa: PLC0415
import asyncio
import json
import logging
import os
import re
import sys
import time
import warnings
from datetime import UTC, datetime, timedelta


def failure(error):
    code = getattr(error, "code", None)
    code = code() if callable(code) else code
    details = getattr(error, "details", "")
    details = details() if callable(details) else details
    details = str(details).lower()
    categories = {
        "REQUEST_ITERATOR": ("iterating requests", "request iterator"),
        "LOCAL_CANCEL": ("locally cancelled", "cancelled by application"),
        "CONNECTION_RESET": ("reset", "rst_stream", "stream removed"),
        "DNS_RESOLUTION": ("dns", "resolve"),
        "CONNECTION_FAILED": ("failed to connect", "connection refused", "socket closed"),
        "DEADLINE": ("deadline", "timed out"),
    }
    return {
        "exception_type": type(error).__name__,
        "grpc_status": getattr(code, "name", None),
        "detail_codes": [name for name, patterns in categories.items() if any(value in details for value in patterns)],
        "rst_codes": sorted(set(re.findall(r"rst_stream with error code ([0-9]{1,2})\b", details))),
    }


async def main():
    warnings.simplefilter("ignore", DeprecationWarning)
    logging.disable(logging.CRITICAL)
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from t_tech.invest.grpc import marketdata_pb2

    from moex_sentinel.adapters.tinvest.market_stream_source import TInvestMarketStreamSource
    from moex_sentinel.services.automaton_brokers import AutomatonBrokerService
    from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as db:
        row = db.execute(
            text(
                "SELECT a.user_broker_id, i.external_instrument_id FROM trading_automations a "
                "JOIN broker_instruments i ON i.id=a.instrument_id AND i.user_broker_id=a.user_broker_id "
                "WHERE a.state='IN_WORK' LIMIT 1"
            )
        ).first()
    if row is None:
        return {"result": "NO_ACTIVE_SOURCE"}
    connection = AutomatonBrokerService(UserBrokerRepository(sessionmaker(engine))).connection(row[0])
    source = TInvestMarketStreamSource(connection.token, connection.target)
    result = {}
    try:
        await source.start()
        if "--tariff" in sys.argv:
            response = await asyncio.wait_for(source._services.users.get_user_tariff(), 15)
            known_streams = (
                "MarketDataStream",
                "TradesStream",
                "OrderStateStream",
                "PositionsStream",
                "PortfolioStream",
            )
            known_methods = ("GetCandles", "GetOrderBook", "GetTradingStatus", "GetUserTariff")
            return {
                "stream_limits": [
                    {
                        "classes": sorted({name for name in known_streams for value in item.streams if name in value}),
                        "limit": item.limit,
                        "open": item.open,
                    }
                    for item in response.stream_limits
                ],
                "relevant_unary_limits": [
                    {"methods": matches, "limit_per_minute": item.limit_per_minute}
                    for item in response.unary_limits
                    if (matches := sorted({name for name in known_methods for value in item.methods if name in value}))
                ],
            }
        await source.replace_subscriptions({row[1]})

        async def history():
            try:
                now = datetime.now(UTC)
                candles = await asyncio.wait_for(source.get_candles(row[1], now - timedelta(minutes=10), now), 15)
                result["history"] = {"count": len(candles)}
            except Exception as error:
                result["history"] = failure(error)

        async def stream():
            iterator = source.events()
            try:
                event = await asyncio.wait_for(anext(iterator), 15)
                result["stream"] = {"event_type": type(event).__name__}
            except Exception as error:
                result["stream"] = failure(error)
            finally:
                await iterator.aclose()

        async def server_stream():
            service = source._services.market_data_stream
            request = marketdata_pb2.MarketDataServerSideStreamRequest(
                subscribe_order_book_request=marketdata_pb2.SubscribeOrderBookRequest(
                    subscription_action=1,
                    instruments=[marketdata_pb2.OrderBookInstrument(instrument_id=row[1], depth=20)],
                )
            )
            call = service.stub.MarketDataServerSideStream(request, metadata=service.metadata)
            try:
                event = await asyncio.wait_for(call.read(), 15)
                result["server_stream"] = {"event_type": type(event).__name__}
            except Exception as error:
                result["server_stream"] = failure(error)
            finally:
                call.cancel()

        async def raw_bidirectional():
            service = source._services.market_data_stream
            hold = asyncio.Event()
            closed = asyncio.Event()

            async def requests():
                try:
                    yield marketdata_pb2.MarketDataRequest(
                        subscribe_order_book_request=marketdata_pb2.SubscribeOrderBookRequest(
                            subscription_action=1,
                            instruments=[marketdata_pb2.OrderBookInstrument(instrument_id=row[1], depth=20)],
                        )
                    )
                    await hold.wait()
                finally:
                    closed.set()

            iterator = requests()
            call = service.stub.MarketDataStream(iterator, metadata=service.metadata)
            started = time.monotonic()
            try:
                event = await asyncio.wait_for(call.read(), 15)
                result["raw_bidirectional"] = {"event_type": type(event).__name__}
            except Exception as error:
                result["raw_bidirectional"] = failure(error)
            finally:
                result["raw_bidirectional"]["latency_ms"] = round((time.monotonic() - started) * 1000)
                call.cancel()
                hold.set()
                await asyncio.wait_for(closed.wait(), 1)
                await iterator.aclose()

        await asyncio.gather(history(), stream(), server_stream(), raw_bidirectional())
    finally:
        await source.close()
        engine.dispose()
    return result


if __name__ == "__main__":
    try:
        output = asyncio.run(main())
    except Exception as error:
        output = {"probe": failure(error)}
    print(json.dumps(output))  # noqa: T201 - diagnostic codes only
