"""Safe additive runtime version metadata and diagnostic observations."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sentinel_contracts.tinvest import BrokerAccessMode, BrokerEnvironment

SafeVersion = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$", max_length=32)]


class RuntimeVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: SafeVersion
    instance_id: UUID
    environment: BrokerEnvironment
    access_mode: BrokerAccessMode


class VersionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: Literal["OBSERVED", "UNKNOWN"] = "UNKNOWN"
    version: SafeVersion | None = None
    received_at: str | None = None
    age_ms: int | None = Field(default=None, ge=0)
    reason: Literal["OBSERVED", "NOT_OBSERVED", "STALE", "NOT_CONFIGURED", "UNAVAILABLE", "INVALID_RESPONSE"]


class ServiceVersions(BaseModel):
    model_config = ConfigDict(frozen=True)

    worker: VersionObservation = Field(default_factory=lambda: VersionObservation(reason="NOT_OBSERVED"))
    analytics: VersionObservation = Field(default_factory=lambda: VersionObservation(reason="NOT_CONFIGURED"))
