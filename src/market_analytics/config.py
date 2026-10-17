"""Runtime settings for the market-only Analytics service."""

from math import isfinite

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalyticsSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False, hide_input_in_errors=True)

    core_url: AnyHttpUrl = Field("http://backend:8000", validation_alias="ANALYTICS_CORE_URL")
    core_http_timeout_seconds: float = Field(2.0, validation_alias="ANALYTICS_CORE_HTTP_TIMEOUT_SECONDS", gt=0)

    @field_validator("core_url")
    @classmethod
    def reject_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("ANALYTICS_CORE_URL must not contain credentials")
        return value

    @model_validator(mode="after")
    def validate_timeout(self) -> "AnalyticsSettings":
        if not isfinite(self.core_http_timeout_seconds) or self.core_http_timeout_seconds <= 0:
            raise ValueError("ANALYTICS_CORE_HTTP_TIMEOUT_SECONDS must be finite and positive")
        return self
