"""Authentication for worker enrollment and scoped internal Core requests."""

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Callable, Protocol
from uuid import UUID

from moex_sentinel.config import Settings


class WorkerAuthenticationError(ValueError):
    """A worker request failed the configured identity or credential checks."""


class RuntimeCredentialRecord(Protocol):
    runtime_instance_id: str
    user_broker_id: str
    worker_id: str
    environment: str
    access_mode: str
    strategy_enabled: bool
    secret_digest: str
    registration_status: str
    generation: int | None
    recovery_only: bool


@dataclass(frozen=True)
class WorkerPrincipal:
    worker_id: str
    runtime_instance_id: UUID
    broker_id: UUID
    environment: str
    access_mode: str
    strategy_enabled: bool
    generation: int | None
    registration_status: str
    recovery_only: bool


class WorkerAuthenticator:
    def __init__(self, settings: Settings, runtime_lookup: Callable[[str], RuntimeCredentialRecord | None]) -> None:
        self._settings = settings
        self._runtime_lookup = runtime_lookup

    def verify_enrollment(self, authorization: str | None) -> None:
        self._verify_worker_token(authorization)

    def authorize_registration(
        self,
        *,
        worker_id: str,
        broker_id: UUID,
        environment: str,
        access_mode: str,
        strategy_enabled: bool,
    ) -> None:
        if (
            worker_id != self._settings.internal_worker_id
            or broker_id != self._settings.internal_worker_broker_id
            or environment != self._settings.application_environment
            or access_mode != self._settings.broker_access_mode
        ):
            raise WorkerAuthenticationError("Worker registration does not match configured scope.")

    def authenticate(
        self,
        *,
        authorization: str | None,
        runtime_instance_header: str | None,
        runtime_secret: str | None,
    ) -> WorkerPrincipal:
        self._verify_worker_token(authorization)
        try:
            runtime_instance_id = UUID(runtime_instance_header or "")
        except ValueError as error:
            raise WorkerAuthenticationError("Worker runtime identity is invalid.") from error
        if runtime_secret is None or not re.fullmatch(r"[0-9a-f]{64}", runtime_secret):
            raise WorkerAuthenticationError("Worker runtime credentials are invalid.")

        record = self._runtime_lookup(str(runtime_instance_id))
        if record is None or record.registration_status != "OWNER" or record.generation is None:
            raise WorkerAuthenticationError("Worker runtime is not an active owner.")
        digest = hashlib.sha256(runtime_secret.encode("ascii")).hexdigest()
        if not hmac.compare_digest(digest, record.secret_digest):
            raise WorkerAuthenticationError("Worker runtime credentials are invalid.")
        if (
            record.runtime_instance_id != str(runtime_instance_id)
            or record.worker_id != self._settings.internal_worker_id
            or record.user_broker_id != str(self._settings.internal_worker_broker_id)
            or record.environment != self._settings.application_environment
            or record.access_mode != self._settings.broker_access_mode
        ):
            raise WorkerAuthenticationError("Worker runtime is outside the configured scope.")
        return WorkerPrincipal(
            worker_id=record.worker_id,
            runtime_instance_id=runtime_instance_id,
            broker_id=UUID(record.user_broker_id),
            environment=record.environment,
            access_mode=record.access_mode,
            strategy_enabled=record.strategy_enabled,
            generation=record.generation,
            registration_status=record.registration_status,
            recovery_only=record.recovery_only,
        )

    def _verify_worker_token(self, authorization: str | None) -> None:
        expected = self._settings.internal_worker_token.get_secret_value()
        if not expected or authorization is None or not authorization.startswith("Bearer "):
            raise WorkerAuthenticationError("Worker authentication failed.")
        supplied = authorization[7:]
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise WorkerAuthenticationError("Worker authentication failed.")
