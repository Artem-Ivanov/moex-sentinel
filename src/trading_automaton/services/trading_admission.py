"""Fail-closed Core checks for PROD intent preparation and submit authorization."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic as system_monotonic
from typing import Protocol
from uuid import UUID

from sentinel_contracts.time import floor_utc_millisecond
from sentinel_contracts.trading_admission import (
    AdmissionView,
    PreRpcAbortProof,
    SubmitAttemptRequest,
    SubmitAttemptView,
    SubmitAuthorizationRequest,
    SubmitAuthorizationResult,
    SubmitDenialReason,
    SubmitDenied,
    SubmitPermit,
    submit_request_fingerprint,
)
from trading_automaton.domain.dtos import DispatchRequest
from trading_automaton.domain.errors import CoreOperationError


class TradingAdmissionCorePort(Protocol):
    def trading_admission(self, broker_id: str) -> AdmissionView: ...
    def authorize_submit(self, request: SubmitAuthorizationRequest) -> SubmitAuthorizationResult: ...
    def submit_attempt(
        self, broker_id: str, idempotency_key: str, request: SubmitAttemptRequest
    ) -> SubmitAttemptView: ...


@dataclass(frozen=True)
class SubmitGate:
    permit: SubmitPermit | None = None
    expires_monotonic: float | None = None
    denial: SubmitDenied | None = None
    abort_proof: PreRpcAbortProof | None = None


class TradingAdmissionService:
    def __init__(
        self,
        core: TradingAdmissionCorePort,
        *,
        broker_id: UUID,
        account_id: str,
        runtime_instance_id: UUID,
        configuration_fingerprint: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = system_monotonic,
    ) -> None:
        self._core = core
        self.broker_id = broker_id
        self.account_id = account_id
        self.runtime_instance_id = runtime_instance_id
        self.configuration_fingerprint = configuration_fingerprint
        self._now = now
        self._monotonic = monotonic

    async def prepare(self, instrument_uid: str) -> AdmissionView | SubmitDenialReason:
        try:
            view = await asyncio.to_thread(self._core.trading_admission, str(self.broker_id))
        except Exception:
            return SubmitDenialReason.CORE_UNAVAILABLE
        if (
            view.broker_id != self.broker_id
            or view.account_id != self.account_id
            or view.owner_runtime_instance_id != self.runtime_instance_id
            or view.owner_generation is None
        ):
            return SubmitDenialReason.NOT_OWNER
        if view.recovery_only:
            return SubmitDenialReason.STOP_REQUIRED
        if view.configuration_fingerprint != self.configuration_fingerprint:
            return SubmitDenialReason.CONFIGURATION_CHANGED
        if view.limits is None or view.limits.canary_uid != instrument_uid:
            return SubmitDenialReason.UID_NOT_ALLOWED
        if not view.effective_can_submit:
            return view.effective_denial_reasons[0] if view.effective_denial_reasons else SubmitDenialReason.DISARMED
        return view

    async def authorize_submit(self, request: DispatchRequest) -> SubmitGate:
        if (
            request.broker_id != str(self.broker_id)
            or request.account_id != self.account_id
            or request.admission_runtime_instance_id != str(self.runtime_instance_id)
            or request.admission_generation is None
            or request.admission_revision is None
            or request.configuration_fingerprint != self.configuration_fingerprint
            or request.market_valid_until is None
        ):
            raise CoreOperationError(retryable=False)
        now = floor_utc_millisecond(self._now())
        submit_deadline = request.submit_deadline or min(request.market_valid_until, now + timedelta(seconds=1))
        if submit_deadline > request.market_valid_until:
            raise CoreOperationError(retryable=False)
        authorization = SubmitAuthorizationRequest(
            broker_id=UUID(request.broker_id),
            account_id=request.account_id,
            automation_id=UUID(request.automation_id),
            idempotency_key=UUID(request.idempotency_key),
            generation=request.admission_generation,
            revision=request.admission_revision,
            configuration_fingerprint=self.configuration_fingerprint,
            instrument_uid=request.instrument_id,
            lot_size=request.lot_size,
            currency=request.reservation_currency,
            side=request.side.value,
            quantity_lots=request.quantity_lots,
            limit_price=request.limit_price,
            market_valid_until=floor_utc_millisecond(request.market_valid_until),
            submit_deadline=floor_utc_millisecond(submit_deadline),
        )
        started = self._monotonic()
        result = await asyncio.to_thread(self._core.authorize_submit, authorization)
        finished = self._monotonic()
        if isinstance(result, SubmitDenied):
            if result.outcome_uncertain:
                raise CoreOperationError(retryable=True)
            return SubmitGate(denial=result)
        if (
            result.runtime_instance_id != self.runtime_instance_id
            or result.idempotency_key != authorization.idempotency_key
            or result.generation != authorization.generation
            or result.revision != authorization.revision
            or result.configuration_fingerprint != self.configuration_fingerprint
            or result.request_fingerprint != submit_request_fingerprint(authorization)
        ):
            raise CoreOperationError(retryable=False)
        permit_seconds = (result.expires_at - result.server_response_at).total_seconds()
        rtt_seconds = finished - started
        usable_seconds = min(
            permit_seconds - rtt_seconds,
            (submit_deadline - floor_utc_millisecond(self._now())).total_seconds(),
            (request.market_valid_until - floor_utc_millisecond(self._now())).total_seconds(),
        )
        if permit_seconds <= 0 or permit_seconds > 1 or usable_seconds <= 0:
            return SubmitGate(
                permit=result,
                abort_proof=self.abort_proof(
                    request,
                    result,
                    (
                        "MARKET_EXPIRED_BEFORE_POST"
                        if self._now() >= request.market_valid_until
                        else "PERMIT_EXPIRED_BEFORE_POST"
                    ),
                ),
            )
        return SubmitGate(permit=result, expires_monotonic=finished + usable_seconds)

    def abort_proof(
        self,
        request: DispatchRequest,
        permit: SubmitPermit,
        reason: str,
    ) -> PreRpcAbortProof:
        return PreRpcAbortProof(
            permit_id=permit.permit_id,
            idempotency_key=permit.idempotency_key,
            request_fingerprint=permit.request_fingerprint,
            runtime_instance_id=self.runtime_instance_id,
            generation=permit.generation,
            configuration_fingerprint=self.configuration_fingerprint,
            aborted_at=floor_utc_millisecond(self._now()),
            reason=reason,
        )

    async def submit_attempt(
        self,
        idempotency_key: str,
        *,
        origin_runtime_instance_id: UUID | None,
        origin_generation: int | None,
    ) -> SubmitAttemptView:
        request = SubmitAttemptRequest(
            original_runtime_instance_id=origin_runtime_instance_id,
            original_generation=origin_generation,
        )
        return await asyncio.to_thread(
            self._core.submit_attempt,
            str(self.broker_id),
            idempotency_key,
            request,
        )
