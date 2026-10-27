"""Bounded, optional alerts derived from safe Core readiness observations."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from time import monotonic
from typing import Literal, Protocol, get_args
from uuid import UUID, uuid4

from sentinel_contracts.time import utc_now_ms
from sentinel_contracts.worker_diagnostics import WorkerObservation

Environment = Literal["TEST", "PROD"]
AccountStatus = Literal["DISARMED", "ARMED", "STOP_REQUIRED", "UNKNOWN"]
PreparationStatus = Literal["READY", "PENDING", "FAILED", "UNKNOWN"]
PreparationReason = Literal[
    "READY",
    "OWNER_MISSING",
    "OWNER_MISMATCH",
    "LEASE_EXPIRED",
    "BASELINE_MISSING",
    "RECONCILIATION_REQUIRED",
    "ARM_REQUIRED",
    "MARKET_STALE",
    "CORE_UNAVAILABLE",
    "NOT_OBSERVED",
    "UNKNOWN",
]
SAFE_PREPARATION_REASONS = frozenset(get_args(PreparationReason))
AlertSignal = Literal[
    "LEGACY_STOP_STATUS",
    "HEARTBEAT_UNKNOWN",
    "CONTROL_STALLED",
    "CONTROL_ERROR",
    "OUTBOX_FAILED",
    "OUTBOX_OLD_PENDING",
    "OUTBOX_UNKNOWN",
    "PREPARATION_FAILED",
    "READINESS_LOST",
    "BROKER_STARTUP_BLOCKED",
    "STARTUP_ADMISSION_BLOCKED",
]
Severity = Literal["CRITICAL", "WARNING"]
AlertState = Literal["ACTIVE", "RECOVERED"]

ALERT_QUEUE_LIMIT = 16
POLL_INTERVAL_SECONDS = 10
STARTUP_UNKNOWN_GRACE_SECONDS = 60
HEARTBEAT_CONFIRMATION_SECONDS = 30
CONTROL_CONFIRMATION_SECONDS = 30
OUTBOX_CONFIRMATION_SECONDS = 60
RETRY_DELAYS_SECONDS = (30, 60, 300)
STARTUP_ADMISSION_BLOCKERS = frozenset(
    {
        "LIMITS_NOT_CONFIGURED",
        "BASELINE_REQUIRED",
        "NOT_OWNER",
        "LEASE_EXPIRED",
        "CONFIGURATION_CHANGED",
        "RECONCILIATION_REQUIRED",
    }
)


@dataclass(frozen=True)
class ReadinessObservation:
    captured_at: datetime
    worker: WorkerObservation
    account_status: AccountStatus
    account_effective_can_submit: bool | None
    recovery_only: bool
    preparation_status: PreparationStatus
    preparation_reason: PreparationReason
    expected_work: bool | None = None
    effective_denial_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlertTransition:
    signal: AlertSignal
    severity: Severity
    state: AlertState
    safe_reason: str
    occurred_at: datetime
    incident_id: UUID

    def message(self, environment: Environment) -> str:
        return (
            f"{environment} | {self.occurred_at.isoformat()} | {self.severity} | "
            f"{self.signal} | {self.state} | {self.safe_reason} | incident {self.incident_id}"
        )


class AlertTransport(Protocol):
    async def send_message(self, text: str) -> None: ...


ObservationCallback = Callable[[], Awaitable[ReadinessObservation]]


@dataclass
class _QueuedAlert:
    event: AlertTransition
    attempts: int = 0
    retry_at: float = 0.0


class TradingReadinessAlertService:
    def __init__(
        self,
        observe: ObservationCallback,
        transport: AlertTransport | None,
        *,
        environment: Environment,
        enabled: bool,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        startup_grace_seconds: float = STARTUP_UNKNOWN_GRACE_SECONDS,
        heartbeat_confirmation_seconds: float = HEARTBEAT_CONFIRMATION_SECONDS,
        control_confirmation_seconds: float = CONTROL_CONFIRMATION_SECONDS,
        outbox_confirmation_seconds: float = OUTBOX_CONFIRMATION_SECONDS,
        retry_delays_seconds: tuple[float, ...] = RETRY_DELAYS_SECONDS,
        queue_limit: int = ALERT_QUEUE_LIMIT,
        clock: Callable[[], datetime] = utc_now_ms,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if enabled and transport is None:
            raise ValueError("enabled readiness alerts require a transport")
        timings = (
            poll_interval_seconds,
            startup_grace_seconds,
            heartbeat_confirmation_seconds,
            control_confirmation_seconds,
            outbox_confirmation_seconds,
        )
        if (
            any(not isfinite(value) or value <= 0 for value in timings)
            or not retry_delays_seconds
            or any(not isfinite(delay) or delay <= 0 for delay in retry_delays_seconds)
            or type(queue_limit) is not int
            or not 0 < queue_limit <= 1024
        ):
            raise ValueError("Readiness alert timings and queue limit are invalid.")
        self._observe = observe
        self._transport = transport
        self._environment = environment
        self._enabled = enabled
        self._poll_interval_seconds = poll_interval_seconds
        self._startup_grace_seconds = startup_grace_seconds
        self._heartbeat_confirmation_seconds = heartbeat_confirmation_seconds
        self._control_confirmation_seconds = control_confirmation_seconds
        self._outbox_confirmation_seconds = outbox_confirmation_seconds
        self._retry_delays_seconds = retry_delays_seconds
        self._queue_limit = queue_limit
        self._clock = clock
        self._monotonic = monotonic_clock
        self._started_at: float | None = None
        self._worker_seen = False
        self._outbox_seen = False
        self._effective_submit_seen = False
        self._broker_ready_seen = False
        self._candidate_since: dict[AlertSignal, float] = {}
        self._active: dict[AlertSignal, AlertTransition] = {}
        self._control_progress_baseline: dict[AlertSignal, tuple[int | None, str | None]] = {}
        self._queue: list[_QueuedAlert] = []
        self._closed = False

    async def observe_once(self) -> tuple[AlertTransition, ...]:
        if self._closed or not self._enabled or self._transport is None:
            return ()
        now = self._monotonic()
        if self._started_at is None:
            self._started_at = now
        try:
            observation = await self._observe()
        except Exception:
            observation = ReadinessObservation(
                captured_at=self._clock(),
                worker=WorkerObservation(reason="NOT_OBSERVED"),
                account_status="UNKNOWN",
                account_effective_can_submit=None,
                recovery_only=False,
                preparation_status="FAILED",
                preparation_reason="CORE_UNAVAILABLE",
            )
        if self._closed:
            return ()
        if observation.account_effective_can_submit is True:
            self._effective_submit_seen = True
        if observation.worker.broker_runtime.decision_eligibility == "READY":
            self._broker_ready_seen = True
        events = self._transitions(observation, now)
        for event in events:
            self._enqueue(event)
        await self._deliver_due(now)
        return tuple(events)

    async def run(self) -> None:
        """Poll until cancelled by Core, which must await this task before closing its executor."""
        while not self._closed:
            await self.observe_once()
            await asyncio.sleep(self._poll_interval_seconds)

    async def close(self) -> None:
        """Stop future observations; the lifecycle owner cancels and awaits ``run`` first."""
        self._closed = True
        self._queue.clear()

    def _transitions(self, observation: ReadinessObservation, now: float) -> list[AlertTransition]:
        candidates = self._candidates(observation)
        current = set(candidates)
        for signal in tuple(self._candidate_since):
            if signal not in current:
                self._candidate_since.pop(signal, None)

        events: list[AlertTransition] = []
        for signal, (reason, severity, confirmation) in candidates.items():
            since = self._candidate_since.setdefault(signal, now)
            if now - since < confirmation:
                continue
            previous = self._active.get(signal)
            if previous is None:
                event = AlertTransition(signal, severity, "ACTIVE", reason, observation.captured_at, uuid4())
                self._active[signal] = event
                if signal in {"CONTROL_STALLED", "CONTROL_ERROR"}:
                    self._control_progress_baseline[signal] = (
                        observation.worker.completed_iterations,
                        observation.worker.last_completed_at,
                    )
                events.append(event)
            elif previous.safe_reason != reason or previous.severity != severity:
                event = AlertTransition(
                    signal, severity, "ACTIVE", reason, observation.captured_at, previous.incident_id
                )
                self._active[signal] = event
                events.append(event)

        for signal, previous in tuple(self._active.items()):
            if signal not in current and self._can_confirm_recovery(signal, observation):
                self._active.pop(signal)
                self._control_progress_baseline.pop(signal, None)
                events.append(
                    AlertTransition(
                        signal,
                        previous.severity,
                        "RECOVERED",
                        "RECOVERED",
                        observation.captured_at,
                        previous.incident_id,
                    )
                )
        return events

    def _candidates(self, value: ReadinessObservation) -> dict[AlertSignal, tuple[str, Severity, float]]:
        result: dict[AlertSignal, tuple[str, Severity, float]] = {}
        if value.account_status == "STOP_REQUIRED":
            result["LEGACY_STOP_STATUS"] = ("LEGACY_STOP_REQUIRED", "WARNING", 0)
        elif value.account_effective_can_submit is False and (self._effective_submit_seen):
            result["READINESS_LOST"] = ("EFFECTIVE_SUBMIT_BLOCKED", "CRITICAL", 0)

        if value.preparation_status == "FAILED" and value.preparation_reason != "ARM_REQUIRED":
            safe_reason = (
                value.preparation_reason
                if isinstance(value.preparation_reason, str) and value.preparation_reason in SAFE_PREPARATION_REASONS
                else "UNKNOWN"
            )
            result["PREPARATION_FAILED"] = (safe_reason, "CRITICAL", 0)

        worker = value.worker
        broker = worker.broker_runtime
        if (
            value.expected_work is True
            and not self._effective_submit_seen
            and value.account_effective_can_submit is False
        ):
            blocker = next(
                (reason for reason in value.effective_denial_reasons if reason in STARTUP_ADMISSION_BLOCKERS), None
            )
            if blocker is not None:
                result["STARTUP_ADMISSION_BLOCKED"] = (
                    blocker,
                    "CRITICAL",
                    max(self._startup_grace_seconds, self._control_confirmation_seconds),
                )
        if (
            value.expected_work
            and not self._broker_ready_seen
            and broker.status == "BLOCKED"
            and broker.reason == "PERMANENT_BROKER_ERROR"
        ):
            result["BROKER_STARTUP_BLOCKED"] = (
                "PERMANENT_BROKER_ERROR",
                "CRITICAL",
                max(self._startup_grace_seconds, self._control_confirmation_seconds),
            )
        if worker.observation == "OBSERVED":
            self._worker_seen = True
        if worker.observation == "UNKNOWN" or worker.reason in {"NOT_OBSERVED", "STALE"}:
            confirmation = self._heartbeat_confirmation_seconds if self._worker_seen else self._startup_grace_seconds
            result["HEARTBEAT_UNKNOWN"] = ("WORKER_UNKNOWN", "CRITICAL", confirmation)
        elif worker.reason == "CONTROL_STALLED":
            result["CONTROL_STALLED"] = ("CONTROL_STALLED", "CRITICAL", self._control_confirmation_seconds)
        elif worker.reason == "ITERATION_FAILED" or worker.last_result == "ERROR":
            result["CONTROL_ERROR"] = ("CONTROL_ERROR", "CRITICAL", self._control_confirmation_seconds)

        queue = worker.outbox
        if queue.observation == "OBSERVED":
            self._outbox_seen = True
        if queue.reason == "FAILED":
            result["OUTBOX_FAILED"] = ("OUTBOX_FAILED", "CRITICAL", 0)
        elif queue.reason == "OLD_PENDING":
            result["OUTBOX_OLD_PENDING"] = ("OUTBOX_OLD_PENDING", "CRITICAL", self._outbox_confirmation_seconds)
        elif queue.reason in {"READ_FAILED", "STALE", "NOT_OBSERVED"}:
            confirmation = self._outbox_confirmation_seconds if self._outbox_seen else self._startup_grace_seconds
            result["OUTBOX_UNKNOWN"] = ("OUTBOX_UNKNOWN", "WARNING", confirmation)
        return result

    def _can_confirm_recovery(self, signal: AlertSignal, value: ReadinessObservation) -> bool:
        if signal == "LEGACY_STOP_STATUS":
            return value.account_status not in {"STOP_REQUIRED", "UNKNOWN"}
        if signal == "READINESS_LOST":
            return value.account_effective_can_submit is True
        if signal == "BROKER_STARTUP_BLOCKED":
            if value.expected_work is False:
                return True
            previous = self._active.get(signal)
            success_at = value.worker.broker_runtime.last_success_at
            return (
                value.expected_work is True
                and value.worker.broker_runtime.status == "RUNNING"
                and previous is not None
                and success_at is not None
                and success_at > previous.occurred_at
            )
        if signal == "STARTUP_ADMISSION_BLOCKED":
            if value.expected_work is False or value.account_effective_can_submit is True:
                return True
            return (
                value.expected_work is True
                and value.account_status != "UNKNOWN"
                and value.account_effective_can_submit is False
                and not any(reason in STARTUP_ADMISSION_BLOCKERS for reason in value.effective_denial_reasons)
            )
        if signal == "PREPARATION_FAILED":
            return value.preparation_status == "READY" or (
                value.preparation_reason == "ARM_REQUIRED" and value.account_status == "DISARMED"
            )
        if signal == "HEARTBEAT_UNKNOWN":
            return value.worker.observation == "OBSERVED" and value.worker.reason not in {
                "STALE",
                "NOT_OBSERVED",
            }
        if signal in {"CONTROL_STALLED", "CONTROL_ERROR"}:
            return self._has_new_control_progress(signal, value.worker)
        if signal in {"OUTBOX_FAILED", "OUTBOX_OLD_PENDING", "OUTBOX_UNKNOWN"}:
            return value.worker.outbox.observation == "OBSERVED" and value.worker.outbox.reason not in {
                "STALE",
                "READ_FAILED",
                "NOT_OBSERVED",
            }
        return False

    def _has_new_control_progress(self, signal: AlertSignal, worker: WorkerObservation) -> bool:
        if (
            worker.observation != "OBSERVED"
            or worker.reason != "CONTROL_PROGRESS"
            or worker.last_result != "COMPLETED"
            or worker.completion_age_ms is None
            or worker.completion_age_ms >= 30_000
        ):
            return False
        previous_count, previous_completed_at = self._control_progress_baseline.get(signal, (None, None))
        count_advanced = (
            previous_count is not None
            and worker.completed_iterations is not None
            and worker.completed_iterations > previous_count
        )
        timestamp_advanced = (
            previous_completed_at is not None
            and worker.last_completed_at is not None
            and worker.last_completed_at > previous_completed_at
        )
        return count_advanced or timestamp_advanced

    def _enqueue(self, event: AlertTransition) -> None:
        for item in self._queue:
            if item.event.signal == event.signal:
                item.event = event
                return
        if len(self._queue) >= self._queue_limit:
            self._queue.pop(0)
        self._queue.append(_QueuedAlert(event))

    async def _deliver_due(self, now: float) -> None:
        if self._transport is None:
            return
        for item in tuple(self._queue):
            if item.retry_at > now:
                continue
            try:
                await self._transport.send_message(item.event.message(self._environment))
            except Exception:
                item.attempts += 1
                delay_index = min(item.attempts - 1, len(self._retry_delays_seconds) - 1)
                item.retry_at = now + self._retry_delays_seconds[delay_index]
            else:
                self._queue.remove(item)
