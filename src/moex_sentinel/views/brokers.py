"""Thin HTTP View for broker settings."""

from fastapi import APIRouter, Request, Response, status
from pydantic import ConfigDict

from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.brokers import (
    BrokerDraftSchema,
    BrokerSchema,
    BrokerSettingsSchema,
    broker_to_schema,
)
from sentinel_contracts.base import PositionalModel

router = APIRouter(prefix="/brokers", tags=["brokers"])


class BrokerConnectionSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")

    broker_id: str
    available: bool
    accounts_count: int


@router.get("", response_model=BrokerSettingsSchema)
async def view_broker_settings(request: Request) -> BrokerSettingsSchema:
    settings = _usecases(request).view_broker_settings.execute()
    return BrokerSettingsSchema.from_domain(settings)


@router.post("", response_model=BrokerSchema, status_code=status.HTTP_201_CREATED)
async def create_broker_settings(request: Request, body: BrokerDraftSchema) -> BrokerSchema:
    broker = _usecases(request).save_broker_settings.execute(None, body.to_domain())
    return broker_to_schema(broker)


@router.put("/{broker_id}", response_model=BrokerSchema)
async def replace_broker_settings(
    broker_id: str,
    request: Request,
    body: BrokerDraftSchema,
) -> BrokerSchema:
    broker = _usecases(request).save_broker_settings.execute(broker_id, body.to_domain())
    return broker_to_schema(broker)


@router.delete("/{broker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_broker_settings(broker_id: str, request: Request) -> Response:
    _usecases(request).delete_broker_settings.execute(broker_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{broker_id}/check", response_model=BrokerConnectionSchema)
async def check_broker_connection(broker_id: str, request: Request) -> BrokerConnectionSchema:
    result = await _usecases(request).check_broker_connection.execute(broker_id)
    return BrokerConnectionSchema.model_validate(result, from_attributes=True)
