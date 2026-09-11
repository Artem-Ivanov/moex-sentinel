"""Read only one market service symbol through Sandbox reflection v1/v1alpha.

Run: .venv/bin/python -m develop.scripts.check_market_reflection
Optional reflection dependency resides only in /tmp/moex-stream-reflection.
Install compatible message definitions without changing project dependencies:
    uv pip install --target /tmp/moex-stream-reflection --no-deps grpcio-reflection==1.76.0
Version 1.83.0 requires protobuf 7; this project currently uses protobuf 6.
No service enumeration, descriptor output, broker orders or database writes.
"""

# ruff: noqa: PLC0415 - keep application imports inside sanitized error handling

import asyncio
import json
import logging
import sys
import warnings

SYMBOL = "tinkoff.public.invest.api.contract.v1.MarketDataStreamService"


async def inspect_service(connection):
    from google.protobuf.descriptor_pb2 import FileDescriptorProto
    from grpc_reflection.v1alpha import reflection_pb2
    from t_tech.invest.channels import create_channel
    from t_tech.invest.metadata import get_metadata

    # The v1/v1alpha request and response message fields used here have identical
    # wire numbers. Only the fully qualified reflection RPC path differs.
    async with create_channel(target=connection.target, force_async=True) as channel:

        async def inspect_version(version):
            request = reflection_pb2.ServerReflectionRequest(file_containing_symbol=SYMBOL)

            async def requests():
                yield request

            rpc = channel.stream_stream(
                f"/grpc.reflection.{version}.ServerReflection/ServerReflectionInfo",
                request_serializer=reflection_pb2.ServerReflectionRequest.SerializeToString,
                response_deserializer=reflection_pb2.ServerReflectionResponse.FromString,
            )
            call = rpc(requests(), metadata=get_metadata(connection.token), timeout=15)
            try:
                response = await call.read()
                kind = response.WhichOneof("message_response")
                if kind == "error_response":
                    return {"reflection_error_code": response.error_response.error_code}
                if kind != "file_descriptor_response":
                    return {"expected_descriptor_response": False}
                result = {
                    "service_exists": False,
                    "market_data_stream_exists": False,
                    "server_side_stream_exists": False,
                }
                for raw in response.file_descriptor_response.file_descriptor_proto:
                    descriptor = FileDescriptorProto.FromString(raw)
                    for service in descriptor.service:
                        if f"{descriptor.package}.{service.name}" != SYMBOL:
                            continue
                        result["service_exists"] = True
                        methods = {method.name for method in service.method}
                        result["market_data_stream_exists"] = "MarketDataStream" in methods
                        result["server_side_stream_exists"] = "MarketDataServerSideStream" in methods
                return result
            except Exception as error:
                # grpc.aio errors expose status via code(); never emit details.
                from grpc import StatusCode

                code = error.code() if callable(getattr(error, "code", None)) else None
                return {"grpc_status": code.name if isinstance(code, StatusCode) else "LOCAL_ERROR"}
            finally:
                call.cancel()

        values = await asyncio.gather(inspect_version("v1"), inspect_version("v1alpha"))
        return dict(zip(("v1", "v1alpha"), values, strict=True))


def main():
    warnings.simplefilter("ignore", DeprecationWarning)
    logging.disable(logging.CRITICAL)
    sys.path.append("/tmp/moex-stream-reflection")  # noqa: S108 - explicitly authorized isolated dependency install
    from dotenv import dotenv_values
    from sqlalchemy import URL, create_engine, text
    from sqlalchemy.orm import sessionmaker

    from moex_sentinel.services.automaton_brokers import AutomatonBrokerService
    from moex_sentinel.storage.repositories.user_brokers import UserBrokerRepository

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
            source_id = db.execute(
                text("SELECT user_broker_id FROM trading_automations WHERE state='IN_WORK' LIMIT 1")
            ).scalar_one_or_none()
        if source_id is None:
            return {"result": "NO_ACTIVE_SOURCE"}
        connection = AutomatonBrokerService(UserBrokerRepository(sessionmaker(engine))).connection(source_id)
        return asyncio.run(inspect_service(connection))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        result = {"result": "LOCAL_ERROR"}
    print(json.dumps(result))  # noqa: T201 - fixed diagnostic booleans and RPC codes only
