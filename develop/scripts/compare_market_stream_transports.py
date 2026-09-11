"""One bounded async/sync and open/half-closed market RPC comparison.

Uses the same configured Sandbox connection and SDK channel/metadata factories.
No broker execution, writes, subscription manager or payload output.
"""

# ruff: noqa: PLC0415
import asyncio
import json
import logging
import os
import re
import threading
import time
import warnings
from importlib.metadata import version


def outcome(error):
    code = getattr(error, "code", None)
    code = code() if callable(code) else code
    details = getattr(error, "details", "")
    details = details() if callable(details) else details
    description = str(details).lower()
    reasons = {
        "UNKNOWN_SERVICE": "unknown service",
        "UNKNOWN_METHOD": "unknown method",
        "METHOD_NOT_FOUND": "method not found",
        "METHOD_NOT_IMPLEMENTED": "method not implemented",
        "HTTP_404": "status code 404",
        "NO_HANDLER": "no handler",
        "UNSUPPORTED": "unsupported",
    }
    return {
        "exception_type": type(error).__name__,
        "grpc_status": getattr(code, "name", None),
        "initial_metadata_present": bool(getattr(error, "initial_metadata", lambda: None)()),
        "trailing_metadata_present": bool(getattr(error, "trailing_metadata", lambda: None)()),
        "detail_codes": [key for key, pattern in reasons.items() if pattern in description],
        "empty_details": not description,
        "received_rst_stream": "received rst_stream" in str(details).lower(),
        "rst_codes": sorted(set(re.findall(r"rst_stream with error code ([0-9]{1,2})\b", str(details).lower()))),
    }


async def compare(connection, instrument):
    from t_tech.invest.async_services import AsyncServices
    from t_tech.invest.channels import create_channel
    from t_tech.invest.grpc import marketdata_pb2
    from t_tech.invest.services import Services

    def request():
        return marketdata_pb2.MarketDataRequest(
            subscribe_order_book_request=marketdata_pb2.SubscribeOrderBookRequest(
                subscription_action=1,
                instruments=[marketdata_pb2.OrderBookInstrument(instrument_id=instrument, depth=20)],
            )
        )

    result = {"grpc_version": version("grpcio"), "sdk_version": version("t-tech-investments")}
    async with create_channel(target=connection.target, force_async=True) as async_channel:
        async_service = AsyncServices(async_channel, token=connection.token).market_data_stream
        with create_channel(target=connection.target) as sync_channel:
            sync_service = Services(sync_channel, token=connection.token).market_data_stream
            result["metadata_equal"] = async_service.metadata == sync_service.metadata

            async def asynchronous(keep_open):
                hold = asyncio.Event()
                closed = asyncio.Event()
                sent = False

                async def requests():
                    nonlocal sent
                    try:
                        sent = True
                        yield request()
                        if keep_open:
                            await hold.wait()
                    finally:
                        closed.set()

                iterator = requests()
                call = async_service.stub.MarketDataStream(iterator, metadata=async_service.metadata, timeout=15)
                started = time.monotonic()
                try:
                    response = await call.read()
                    observed = {"event_type": type(response).__name__}
                except Exception as error:
                    observed = outcome(error)
                finally:
                    latency = round((time.monotonic() - started) * 1000)
                    call.cancel()
                    hold.set()
                    if sent:
                        await asyncio.wait_for(closed.wait(), 1)
                    await iterator.aclose()
                return {"request_yielded": sent, "latency_ms": latency, **observed}

            def synchronous(keep_open):
                hold = threading.Event()
                closed = threading.Event()
                sent = False

                def requests():
                    nonlocal sent
                    try:
                        sent = True
                        yield request()
                        if keep_open:
                            hold.wait(16)
                    finally:
                        closed.set()

                iterator = requests()
                call = sync_service.stub.MarketDataStream(iterator, metadata=sync_service.metadata, timeout=15)
                started = time.monotonic()
                try:
                    response = next(call)
                    observed = {"event_type": type(response).__name__}
                except Exception as error:
                    observed = outcome(error)
                finally:
                    latency = round((time.monotonic() - started) * 1000)
                    call.cancel()
                    hold.set()
                    if sent:
                        closed.wait(1)
                    iterator.close()
                return {"request_yielded": sent, "latency_ms": latency, **observed}

            values = await asyncio.gather(
                asynchronous(True),
                asynchronous(False),
                asyncio.to_thread(synchronous, True),
                asyncio.to_thread(synchronous, False),
            )
            result.update(
                zip(("async_open", "async_half_closed", "sync_open", "sync_half_closed"), values, strict=True)
            )
    return result


def main():
    warnings.simplefilter("ignore", DeprecationWarning)
    logging.disable(logging.CRITICAL)
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from moex_sentinel.services.automaton_brokers import AutomatonBrokerService
    from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository

    engine = create_engine(os.environ["DATABASE_URL"])
    try:
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
        return asyncio.run(compare(connection, row[1]))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        output = main()
    except Exception as error:
        output = {"probe": outcome(error)}
    print(json.dumps(output))  # noqa: T201 - fixed diagnostic fields only
