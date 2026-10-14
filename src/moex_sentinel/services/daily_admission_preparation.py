"""One-process scheduler for safe daily PROD admission preparation."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from moex_sentinel.domain.daily_admission_preparation import (
    DailyAdmissionPreparationReason,
    DailyAdmissionPreparationStatus,
    DailyAdmissionPreparationView,
)
from sentinel_contracts.time import utc_now_ms

LOGGER = logging.getLogger(__name__)
_RETRY_DELAYS_SECONDS = (60, 120, 300)
_DAY_CHECK_SECONDS = 60
_ATTEMPT_TIMEOUT_SECONDS = 30


class DailyAdmissionPreparationService:
    def __init__(
        self,
        admission,
        *,
        broker_id: str | None,
        enabled: bool,
        environment: str,
        canary_uid: str,
        access_mode: str = "TRADE",
        clock: Callable[[], datetime] = utc_now_ms,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._admission = admission
        self._broker_id = broker_id
        self._enabled = enabled
        self._environment = environment
        self._access_mode = access_mode
        self._canary_uid = canary_uid
        self._clock = clock
        self._sleep = sleep
        self._observation: DailyAdmissionPreparationView | None = None

    async def prepare_once(self) -> DailyAdmissionPreparationView:
        if not self._enabled:
            result = self._empty(
                DailyAdmissionPreparationStatus.DISABLED,
                DailyAdmissionPreparationReason.DAILY_PREPARATION_DISABLED,
            )
        elif self._environment != "PROD" or self._access_mode != "TRADE" or not self._broker_id:
            result = self._blocked(DailyAdmissionPreparationReason.SCOPE_UNAVAILABLE)
        else:
            try:
                result = await self._admission.prepare_daily(self._broker_id, canary_uid=self._canary_uid)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("Daily admission preparation failed: CORE_UNAVAILABLE")
                result = self._blocked(DailyAdmissionPreparationReason.CORE_UNAVAILABLE)
        self._observation = result
        return result

    async def snapshot(self, broker_id: str) -> DailyAdmissionPreparationView:
        if not self._broker_id or broker_id != self._broker_id:
            raise ValueError("Daily preparation is unavailable for this broker scope.")
        if not self._enabled:
            return self._empty(
                DailyAdmissionPreparationStatus.DISABLED,
                DailyAdmissionPreparationReason.DAILY_PREPARATION_DISABLED,
            )
        if self._environment != "PROD" or self._access_mode != "TRADE":
            return self._blocked(DailyAdmissionPreparationReason.SCOPE_UNAVAILABLE)
        observation = self._observation
        if observation is None:
            return self._empty(
                DailyAdmissionPreparationStatus.WAITING,
                DailyAdmissionPreparationReason.NOT_OBSERVED,
            )
        try:
            return await self._admission.validate_daily_preparation(broker_id, observation)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._waiting(DailyAdmissionPreparationReason.CORE_UNAVAILABLE)

    async def run(self) -> None:
        if not self._enabled or self._environment != "PROD" or self._access_mode != "TRADE" or not self._broker_id:
            return
        failures = 0
        checked_day = None
        retry_at = None
        while True:
            now = self._clock()
            current_day = now.astimezone(ZoneInfo("Europe/Moscow")).date()
            if current_day != checked_day:
                checked_day = current_day
                failures = 0
                retry_at = now
            if retry_at is not None and now < retry_at:
                next_midnight = datetime.combine(
                    current_day + timedelta(days=1), time.min, ZoneInfo("Europe/Moscow")
                ).astimezone(now.tzinfo)
                wait_until_retry = (retry_at - now).total_seconds()
                wait_until_day_check = (next_midnight - now).total_seconds()
                await self._sleep(min(_DAY_CHECK_SECONDS, wait_until_retry, wait_until_day_check))
                continue
            try:
                async with asyncio.timeout(_ATTEMPT_TIMEOUT_SECONDS):
                    observation = await self.prepare_once()
            except TimeoutError:
                observation = self._blocked(DailyAdmissionPreparationReason.CORE_UNAVAILABLE)
                self._observation = observation
            if observation.status in {
                DailyAdmissionPreparationStatus.BLOCKED,
                DailyAdmissionPreparationStatus.WAITING,
            }:
                failures += 1
                retry_delay = _RETRY_DELAYS_SECONDS[min(failures - 1, len(_RETRY_DELAYS_SECONDS) - 1)]
            else:
                failures = 0
                retry_delay = _DAY_CHECK_SECONDS
            retry_at = self._clock() + timedelta(seconds=retry_delay)

    def _empty(
        self,
        status: DailyAdmissionPreparationStatus,
        reason: DailyAdmissionPreparationReason,
    ) -> DailyAdmissionPreparationView:
        return DailyAdmissionPreparationView(
            moscow_day=None,
            captured_at=None,
            status=status,
            reason=reason,
            baseline_id=None,
            owner_runtime_instance_id=None,
            owner_generation=None,
            configuration_fingerprint=None,
            reconciliation_id=None,
            reconciliation_valid_until=None,
            suggested_limits=None,
            risk_reasons=(),
        )

    def _waiting(self, reason: DailyAdmissionPreparationReason) -> DailyAdmissionPreparationView:
        return self._empty(DailyAdmissionPreparationStatus.WAITING, reason)

    def _blocked(self, reason: DailyAdmissionPreparationReason) -> DailyAdmissionPreparationView:
        now = self._clock()
        return DailyAdmissionPreparationView(
            moscow_day=now.astimezone(ZoneInfo("Europe/Moscow")).date(),
            captured_at=now,
            status=DailyAdmissionPreparationStatus.BLOCKED,
            reason=reason,
            baseline_id=None,
            owner_runtime_instance_id=None,
            owner_generation=None,
            configuration_fingerprint=None,
            reconciliation_id=None,
            reconciliation_valid_until=None,
            suggested_limits=None,
            risk_reasons=(),
        )
