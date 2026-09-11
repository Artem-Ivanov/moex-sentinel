"""Read-only Node HTTP/2 control using the configured Sandbox and public SDK CA."""

# ruff: noqa: PLC0415 - suppress SDK deprecations before importing transport code

import base64
import json
import os
import shutil
import subprocess
import sys
import warnings
from importlib.resources import files
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import URL, create_engine, text
from sqlalchemy.orm import sessionmaker

from moex_sentinel.services.automaton_brokers import AutomatonBrokerService
from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository


def main():
    warnings.simplefilter("ignore", DeprecationWarning)
    from t_tech.invest.grpc import marketdata_pb2
    from t_tech.invest.metadata import get_metadata

    values = dotenv_values(".env")
    url = URL.create(
        "postgresql+psycopg",
        username=values["POSTGRES_USER"],
        password=values["POSTGRES_PASSWORD"],
        host="127.0.0.1",
        port=int(values.get("POSTGRES_PORT") or "55432"),
        database=values["POSTGRES_DB"],
    )
    engine = create_engine(url)
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
        request = marketdata_pb2.MarketDataRequest(
            subscribe_order_book_request=marketdata_pb2.SubscribeOrderBookRequest(
                subscription_action=1,
                instruments=[marketdata_pb2.OrderBookInstrument(instrument_id=row[1], depth=20)],
            )
        )
        env = os.environ.copy()
        env.update(
            {
                "MARKET_PROBE_TARGET": connection.target,
                "MARKET_PROBE_METADATA": json.dumps(dict(get_metadata(connection.token))),
                "MARKET_PROBE_REQUEST": base64.b64encode(request.SerializeToString()).decode(),
                "MARKET_PROBE_CA": str(files("t_tech.invest.certs").joinpath("RussianTrustedRootCA.pem")),
                "MARKET_PROBE_CONTROLS": "1" if "--controls" in sys.argv else "0",
                "MARKET_PROBE_BOOK_REQUEST": base64.b64encode(
                    marketdata_pb2.GetOrderBookRequest(instrument_id=row[1], depth=20).SerializeToString()
                ).decode(),
            }
        )
        result = subprocess.run(
            [shutil.which("node") or "/usr/bin/node", str(Path(__file__).with_suffix(".mjs"))],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode:
            return {"error": "NODE_PROBE_FAILED", "exit_code": result.returncode}
        return json.loads(result.stdout)
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        result = main()
    except Exception as error:
        result = {"error_type": type(error).__name__}
    print(json.dumps(result))  # noqa: T201 - fixed diagnostic fields only
