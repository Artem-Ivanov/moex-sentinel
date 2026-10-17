import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from moex_sentinel.services.trading_readiness_alerts import (
    ALERT_QUEUE_LIMIT,
    AlertTransition,
    ReadinessObservation,
    TradingReadinessAlertService,
)
from sentinel_contracts.worker_diagnostics import OutboxObservation, WorkerObservation

NOW = datetime(2026, 10, 14, 10, tzinfo=UTC)


def observation(**changes):
    values = {
        "captured_at": NOW,
        "worker": WorkerObservation(
            reason="CONTROL_PROGRESS",
            observation="OBSERVED",
            status="OK",
            completed_iterations=1,
            last_completed_at=NOW.isoformat(),
            completion_age_ms=0,
            last_finished_at=NOW.isoformat(),
            last_result="COMPLETED",
            outbox=OutboxObservation(observation="OBSERVED", status="OK", reason="CLEAR"),
        ),
        "account_status": "ARMED",
        "account_effective_can_submit": True,
        "recovery_only": False,
        "preparation_status": "READY",
        "preparation_reason": "READY",
    }
    values.update(changes)
    return ReadinessObservation(**values)


class Transport:
    def __init__(self):
        self.messages = []

    async def send_message(self, text):
        self.messages.append(text)


def test_disabled_service_does_not_observe_or_send():
    calls = []
    service = TradingReadinessAlertService(
        lambda: calls.append("observe"), Transport(), environment="PROD", enabled=False, clock=lambda: NOW
    )

    asyncio.run(service.observe_once())

    assert calls == []


def test_enabled_service_requires_a_transport():
    with pytest.raises(ValueError, match="enabled readiness alerts require a transport"):
        TradingReadinessAlertService(
            lambda: _resolved(observation()), None, environment="PROD", enabled=True, clock=lambda: NOW
        )


def test_close_prevents_inflight_observation_from_sending():
    started = asyncio.Event()
    release = asyncio.Event()
    sent = []

    async def blocked_observer():
        started.set()
        await release.wait()
        return observation(account_status="STOP_REQUIRED")

    class CapturingTransport:
        async def send_message(self, text):
            sent.append(text)

    service = TradingReadinessAlertService(
        blocked_observer, CapturingTransport(), environment="PROD", enabled=True, clock=lambda: NOW
    )

    async def scenario():
        pending = asyncio.create_task(service.observe_once())
        await started.wait()
        await service.close()
        release.set()
        assert await pending == ()
        assert await service.observe_once() == ()

    asyncio.run(scenario())

    assert sent == []


def test_run_propagates_cancellation_to_its_lifecycle_owner():
    service = TradingReadinessAlertService(
        lambda: _resolved(observation()), Transport(), environment="PROD", enabled=True, clock=lambda: NOW
    )

    async def scenario():
        task = asyncio.create_task(service.run())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_legacy_stop_status_is_diagnostic_warning_and_recovers_without_manual_permission():
    current = [observation(account_status="STOP_REQUIRED", recovery_only=True)]
    transport = Transport()
    service = TradingReadinessAlertService(
        lambda: _resolved(current[0]), transport, environment="PROD", enabled=True, clock=lambda: NOW
    )

    async def scenario():
        stopped = await service.observe_once()
        repeated = await service.observe_once()
        current[0] = observation()
        recovered = await service.observe_once()
        recovered_incident = recovered[0].incident_id
        current[0] = observation(account_status="STOP_REQUIRED", recovery_only=True)
        restarted = await service.observe_once()
        return stopped, repeated, recovered, recovered_incident, restarted

    stopped, repeated, recovered, recovered_incident, restarted = asyncio.run(scenario())

    assert [event.state for event in stopped] == ["ACTIVE"]
    assert stopped[0].signal == "LEGACY_STOP_STATUS"
    assert stopped[0].severity == "WARNING"
    assert repeated == ()
    assert [event.state for event in recovered] == ["RECOVERED"]
    assert [event.state for event in restarted] == ["ACTIVE"]
    assert restarted[0].incident_id != recovered_incident
    assert len(transport.messages) == 3
    assert "LEGACY_STOP_REQUIRED" in transport.messages[0]
    assert "RECOVERED" in transport.messages[1]
    assert "RESUMED" not in " ".join(transport.messages)


def test_legacy_recovery_flag_does_not_report_manual_stop_when_account_is_disarmed():
    current = [observation(account_status="DISARMED", recovery_only=True)]
    transport = Transport()
    service = TradingReadinessAlertService(
        lambda: _resolved(current[0]), transport, environment="PROD", enabled=True, clock=lambda: NOW
    )

    events = asyncio.run(service.observe_once())

    assert events == ()
    assert transport.messages == []


def test_startup_unknown_waits_for_sixty_second_grace():
    ticks = [0.0]
    transport = Transport()
    service = TradingReadinessAlertService(
        lambda: _resolved(
            observation(
                worker=WorkerObservation(
                    reason="NOT_OBSERVED",
                    outbox=OutboxObservation(observation="OBSERVED", status="OK", reason="CLEAR"),
                )
            )
        ),
        transport,
        environment="PROD",
        enabled=True,
        clock=lambda: NOW,
        monotonic_clock=lambda: ticks[0],
    )

    async def scenario():
        await service.observe_once()
        ticks[0] = 59
        await service.observe_once()
        ticks[0] = 60
        await service.observe_once()

    asyncio.run(scenario())

    assert len(transport.messages) == 1
    assert "HEARTBEAT_UNKNOWN" in transport.messages[0]


def test_lost_worker_heartbeat_waits_thirty_seconds_after_ttl_unknown():
    ticks = [0.0]
    transport = Transport()
    current = [observation()]
    service = TradingReadinessAlertService(
        lambda: _resolved(current[0]),
        transport,
        environment="PROD",
        enabled=True,
        clock=lambda: NOW,
        monotonic_clock=lambda: ticks[0],
    )

    async def scenario():
        await service.observe_once()
        current[0] = observation(worker=WorkerObservation(reason="STALE"))
        ticks[0] = 30
        await service.observe_once()
        ticks[0] = 59
        await service.observe_once()
        assert transport.messages == []
        ticks[0] = 60
        await service.observe_once()

    asyncio.run(scenario())

    assert len(transport.messages) == 1
    assert "HEARTBEAT_UNKNOWN" in transport.messages[0]


def test_worker_unknown_does_not_recover_a_stall_without_observed_progress():
    ticks = [0.0]
    current = [
        observation(
            worker=WorkerObservation(
                observation="OBSERVED",
                status="DEGRADED",
                reason="CONTROL_STALLED",
                completed_iterations=1,
                last_completed_at=NOW.isoformat(),
                completion_age_ms=30_000,
                last_finished_at=NOW.isoformat(),
                last_result="COMPLETED",
                outbox=OutboxObservation(observation="OBSERVED", status="OK", reason="CLEAR"),
            )
        )
    ]
    transport = Transport()
    service = TradingReadinessAlertService(
        lambda: _resolved(current[0]),
        transport,
        environment="PROD",
        enabled=True,
        clock=lambda: NOW,
        monotonic_clock=lambda: ticks[0],
    )

    async def scenario():
        await service.observe_once()
        ticks[0] = 30
        await service.observe_once()
        current[0] = observation(worker=WorkerObservation(reason="STALE"))
        ticks[0] = 31
        events_while_unknown = await service.observe_once()
        progress_at = NOW + timedelta(seconds=32)
        current[0] = observation(
            captured_at=progress_at,
            worker=WorkerObservation(
                observation="OBSERVED",
                status="OK",
                reason="CONTROL_PROGRESS",
                completed_iterations=2,
                last_completed_at=progress_at.isoformat(),
                completion_age_ms=0,
                last_finished_at=progress_at.isoformat(),
                last_result="COMPLETED",
                outbox=OutboxObservation(observation="OBSERVED", status="OK", reason="CLEAR"),
            ),
        )
        ticks[0] = 32
        events_after_progress = await service.observe_once()
        return events_while_unknown, events_after_progress

    events_while_unknown, events_after_progress = asyncio.run(scenario())

    assert "RECOVERED" not in {event.state for event in events_while_unknown}
    assert [event.state for event in events_after_progress] == ["RECOVERED"]
    assert events_after_progress[0].signal == "CONTROL_STALLED"


@pytest.mark.parametrize(
    "intermediate_worker",
    [
        WorkerObservation(
            observation="OBSERVED",
            status="DEGRADED",
            reason="ITERATION_FAILED",
            completed_iterations=1,
            last_completed_at=NOW.isoformat(),
            completion_age_ms=30_001,
            last_finished_at=NOW.isoformat(),
            last_result="ERROR",
            error_code="ITERATION_FAILED",
            outbox=OutboxObservation(observation="OBSERVED", status="OK", reason="CLEAR"),
        ),
        WorkerObservation(
            observation="OBSERVED",
            status="DEGRADED",
            reason="OUTBOX_BLOCKED",
            completed_iterations=1,
            last_completed_at=NOW.isoformat(),
            completion_age_ms=30_001,
            last_finished_at=NOW.isoformat(),
            last_result="OUTBOX_BLOCKED",
            outbox=OutboxObservation(observation="OBSERVED", status="OK", reason="CLEAR"),
        ),
    ],
)
def test_stall_recovers_only_after_new_successful_control_progress(intermediate_worker):
    ticks = [0.0]
    current = [
        observation(
            worker=WorkerObservation(
                observation="OBSERVED",
                status="DEGRADED",
                reason="CONTROL_STALLED",
                completed_iterations=1,
                last_completed_at=NOW.isoformat(),
                completion_age_ms=30_000,
                last_finished_at=NOW.isoformat(),
                last_result="COMPLETED",
                outbox=OutboxObservation(observation="OBSERVED", status="OK", reason="CLEAR"),
            )
        )
    ]
    transport = Transport()
    service = TradingReadinessAlertService(
        lambda: _resolved(current[0]),
        transport,
        environment="PROD",
        enabled=True,
        clock=lambda: NOW,
        monotonic_clock=lambda: ticks[0],
    )

    async def scenario():
        await service.observe_once()
        ticks[0] = 30
        activated = await service.observe_once()
        incident_id = activated[0].incident_id
        current[0] = observation(worker=intermediate_worker)
        ticks[0] = 31
        intermediate_events = await service.observe_once()
        successful_at = NOW + timedelta(seconds=32)
        current[0] = observation(
            captured_at=successful_at,
            worker=WorkerObservation(
                observation="OBSERVED",
                status="OK",
                reason="CONTROL_PROGRESS",
                completed_iterations=2,
                last_completed_at=successful_at.isoformat(),
                completion_age_ms=0,
                last_finished_at=successful_at.isoformat(),
                last_result="COMPLETED",
                outbox=OutboxObservation(observation="OBSERVED", status="OK", reason="CLEAR"),
            ),
        )
        ticks[0] = 32
        recovered = await service.observe_once()
        return incident_id, intermediate_events, recovered

    incident_id, intermediate_events, recovered = asyncio.run(scenario())

    assert "RECOVERED" not in {event.state for event in intermediate_events}
    assert [event.signal for event in recovered] == ["CONTROL_STALLED"]
    assert [event.state for event in recovered] == ["RECOVERED"]
    assert recovered[0].incident_id == incident_id


def test_recovery_coalesces_with_active_alert_while_first_delivery_is_pending():
    ticks = [0.0]
    current = [observation(account_status="STOP_REQUIRED")]
    attempts = []

    class FailOnce:
        async def send_message(self, text):
            attempts.append(text)
            if len(attempts) == 1:
                raise RuntimeError("private transport detail")

    service = TradingReadinessAlertService(
        lambda: _resolved(current[0]),
        FailOnce(),
        environment="PROD",
        enabled=True,
        clock=lambda: NOW,
        monotonic_clock=lambda: ticks[0],
    )

    async def scenario():
        await service.observe_once()
        current[0] = observation()
        ticks[0] = 1
        await service.observe_once()
        ticks[0] = 30
        await service.observe_once()

    asyncio.run(scenario())

    assert len(attempts) == 2
    assert "RECOVERED" in attempts[1]
    assert "ACTIVE" not in attempts[1]


def test_failed_delivery_retries_after_bounded_schedule_without_marking_alert_sent():
    ticks = [0.0]
    attempts = []

    class FailingTransport:
        async def send_message(self, text):
            attempts.append(ticks[0])
            raise RuntimeError("secret must not escape")

    service = TradingReadinessAlertService(
        lambda: _resolved(observation(account_status="STOP_REQUIRED")),
        FailingTransport(),
        environment="PROD",
        enabled=True,
        clock=lambda: NOW,
        monotonic_clock=lambda: ticks[0],
    )

    async def scenario():
        for tick in (0, 29, 30, 89, 90, 389, 390):
            ticks[0] = tick
            await service.observe_once()

    asyncio.run(scenario())

    assert attempts == [0, 30, 90, 390]


def test_configured_alert_queue_limit_and_retry_delay_reach_delivery_queue():
    ticks = [0.0]

    class FailingTransport:
        async def send_message(self, _text):
            raise RuntimeError("offline")

    service = TradingReadinessAlertService(
        lambda: _resolved(observation()),
        FailingTransport(),
        environment="PROD",
        enabled=True,
        retry_delays_seconds=(2, 5),
        queue_limit=2,
        clock=lambda: NOW,
        monotonic_clock=lambda: ticks[0],
    )
    for index in range(3):
        service._enqueue(
            AlertTransition(
                signal=f"SIGNAL_{index}",
                severity="WARNING",
                state="ACTIVE",
                safe_reason="UNKNOWN",
                occurred_at=NOW,
                incident_id=uuid4(),
            )
        )

    async def deliver():
        await service._deliver_due(0)

    asyncio.run(deliver())

    assert len(service._queue) == 2
    assert service._queue[0].event.signal == "SIGNAL_1"
    assert service._queue[0].retry_at == 2


def test_alert_queue_never_exceeds_sixteen_entries():
    service = TradingReadinessAlertService(
        lambda: _resolved(observation()), Transport(), environment="PROD", enabled=True, clock=lambda: NOW
    )

    for index in range(ALERT_QUEUE_LIMIT + 5):
        service._enqueue(
            AlertTransition(
                signal=f"SIGNAL_{index}",
                severity="WARNING",
                state="ACTIVE",
                safe_reason="UNKNOWN",
                occurred_at=NOW,
                incident_id=uuid4(),
            )
        )

    assert len(service._queue) == ALERT_QUEUE_LIMIT
    assert service._queue[0].event.signal == "SIGNAL_5"


def test_material_safe_reason_change_updates_active_incident():
    current = [observation(preparation_status="FAILED", preparation_reason="CORE_UNAVAILABLE")]
    transport = Transport()
    service = TradingReadinessAlertService(
        lambda: _resolved(current[0]), transport, environment="PROD", enabled=True, clock=lambda: NOW
    )

    async def scenario():
        first = await service.observe_once()
        current[0] = observation(preparation_status="FAILED", preparation_reason="MARKET_STALE")
        changed = await service.observe_once()
        return first, changed

    first, changed = asyncio.run(scenario())

    initial_preparation = next(event for event in first if event.signal == "PREPARATION_FAILED")
    changed_preparation = next(event for event in changed if event.signal == "PREPARATION_FAILED")
    assert initial_preparation.safe_reason == "CORE_UNAVAILABLE"
    assert changed_preparation.safe_reason == "MARKET_STALE"
    assert initial_preparation.incident_id == changed_preparation.incident_id


def test_preparation_pending_and_initial_disarmed_arm_required_are_not_readiness_alerts():
    transport = Transport()
    pending_armed = observation(preparation_status="PENDING", preparation_reason="RECONCILIATION_REQUIRED")
    initial_disarmed = observation(
        account_status="DISARMED",
        account_effective_can_submit=False,
        preparation_status="FAILED",
        preparation_reason="ARM_REQUIRED",
    )
    service = TradingReadinessAlertService(
        lambda: _resolved(pending_armed), transport, environment="PROD", enabled=True, clock=lambda: NOW
    )
    disarmed_service = TradingReadinessAlertService(
        lambda: _resolved(initial_disarmed), transport, environment="PROD", enabled=True, clock=lambda: NOW
    )

    async def scenario():
        pending_events = await service.observe_once()
        disarmed_events = await disarmed_service.observe_once()
        return pending_events, disarmed_events

    pending_events, disarmed_events = asyncio.run(scenario())

    assert "READINESS_LOST" not in {event.signal for event in pending_events + disarmed_events}
    assert transport.messages == []


def test_confirmed_effective_submit_loss_alerts_even_when_preparation_is_ready():
    current = [observation()]
    transport = Transport()
    service = TradingReadinessAlertService(
        lambda: _resolved(current[0]), transport, environment="PROD", enabled=True, clock=lambda: NOW
    )

    async def scenario():
        await service.observe_once()
        current[0] = observation(account_effective_can_submit=False)
        return await service.observe_once()

    events = asyncio.run(scenario())

    assert [event.signal for event in events] == ["READINESS_LOST"]
    assert events[0].safe_reason == "EFFECTIVE_SUBMIT_BLOCKED"


@pytest.mark.parametrize("malicious_reason", ["secret-account-token", ["secret-account-token"]])
def test_untrusted_preparation_reason_is_replaced_before_alert_text(malicious_reason):
    value = observation(preparation_status="FAILED", preparation_reason=malicious_reason)
    transport = Transport()
    service = TradingReadinessAlertService(
        lambda: _resolved(value), transport, environment="PROD", enabled=True, clock=lambda: NOW
    )

    events = asyncio.run(service.observe_once())

    assert next(event for event in events if event.signal == "PREPARATION_FAILED").safe_reason == "UNKNOWN"
    assert "secret-account-token" not in transport.messages[0]


def test_callback_failure_uses_safe_reason_without_logging_raw_exception(caplog):
    transport = Transport()

    async def broken_observer():
        raise RuntimeError("secret-account-identifier")

    service = TradingReadinessAlertService(
        broken_observer, transport, environment="PROD", enabled=True, clock=lambda: NOW
    )

    asyncio.run(service.observe_once())

    assert len(transport.messages) == 1
    assert "CORE_UNAVAILABLE" in transport.messages[0]
    assert "secret-account-identifier" not in caplog.text


def test_failed_outbox_alerts_immediately_but_old_pending_waits_for_confirmation():
    ticks = [0.0]
    current = [
        observation(
            worker=WorkerObservation(
                observation="OBSERVED",
                status="DEGRADED",
                reason="CONTROL_PROGRESS",
                outbox=OutboxObservation(observation="OBSERVED", status="DEGRADED", reason="FAILED"),
            )
        )
    ]
    failed_transport = Transport()
    failed_service = TradingReadinessAlertService(
        lambda: _resolved(current[0]),
        failed_transport,
        environment="PROD",
        enabled=True,
        clock=lambda: NOW,
        monotonic_clock=lambda: ticks[0],
    )

    async def immediate_failure():
        return await failed_service.observe_once()

    failed_events = asyncio.run(immediate_failure())
    assert [event.signal for event in failed_events] == ["OUTBOX_FAILED"]

    current[0] = observation(
        worker=WorkerObservation(
            observation="OBSERVED",
            status="DEGRADED",
            reason="CONTROL_PROGRESS",
            outbox=OutboxObservation(
                observation="OBSERVED", status="DEGRADED", reason="OLD_PENDING", oldest_pending_age_ms=60_000
            ),
        )
    )
    delayed_transport = Transport()
    delayed_service = TradingReadinessAlertService(
        lambda: _resolved(current[0]),
        delayed_transport,
        environment="PROD",
        enabled=True,
        clock=lambda: NOW,
        monotonic_clock=lambda: ticks[0],
    )

    async def pending_scenario():
        await delayed_service.observe_once()
        ticks[0] = 59
        await delayed_service.observe_once()
        before_confirmation = len(delayed_transport.messages)
        ticks[0] = 60
        await delayed_service.observe_once()
        return before_confirmation

    before_confirmation = asyncio.run(pending_scenario())

    assert before_confirmation == 0
    assert len(delayed_transport.messages) == 1
    assert "OUTBOX_OLD_PENDING" in delayed_transport.messages[0]


async def _resolved(value):
    return value
