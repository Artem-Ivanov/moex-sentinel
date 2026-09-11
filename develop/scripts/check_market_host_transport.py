"""Run the same read-only transport comparison on the host with local Core config."""

import json
import os

from dotenv import dotenv_values
from sqlalchemy import URL

from develop.scripts.compare_market_stream_transports import main, outcome


def run():
    values = dotenv_values(".env")
    os.environ["DATABASE_URL"] = URL.create(
        "postgresql+psycopg",
        username=values["POSTGRES_USER"],
        password=values["POSTGRES_PASSWORD"],
        host="127.0.0.1",
        port=int(values.get("POSTGRES_PORT") or "55432"),
        database=values["POSTGRES_DB"],
    ).render_as_string(hide_password=False)
    return main()


if __name__ == "__main__":
    try:
        result = run()
    except Exception as error:
        result = {"probe": outcome(error)}
    print(json.dumps(result))  # noqa: T201 - diagnostic codes only
