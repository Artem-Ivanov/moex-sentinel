"""Small helpers for deadlines tied to durable order intent facts."""

import asyncio
from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sentinel_contracts.time import floor_utc_millisecond, is_historical

CONFIRMED_NONTERMINAL_BROKER_STATUSES = frozenset({"ACCEPTED", "PARTIALLY_FILLED"})


def is_confirmed_nonterminal_status(status: str) -> bool:
    return status in CONFIRMED_NONTERMINAL_BROKER_STATUSES


async def bounded_broker_call(awaitable: Awaitable[Any], timeout_seconds: float) -> Any:
    """Bound an RPC without waiting for cancellation-ignoring SDK coroutines."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise TimeoutError("Broker request exceeded its time budget")
    return task.result()


def _consume_task_result(task: asyncio.Future[object]) -> None:
    if not task.cancelled():
        task.exception()


def intent_deadline(intent: object, *, now: datetime) -> datetime | None:
    """Return a trusted deadline, or None when durable timing proof is incomplete."""
    started_at = getattr(intent, "dispatch_started_at", None)
    snapshot = getattr(intent, "strategy_snapshot", None)
    ttl_seconds = snapshot.get("order_ttl_seconds") if isinstance(snapshot, dict) else None
    if (
        not isinstance(started_at, datetime)
        or started_at.tzinfo is None
        or started_at.utcoffset() != timedelta(0)
        or not isinstance(ttl_seconds, int)
        or isinstance(ttl_seconds, bool)
        or ttl_seconds <= 0
        or now.tzinfo is None
        or now.utcoffset() != timedelta(0)
    ):
        return None
    started_at = floor_utc_millisecond(started_at)
    current = floor_utc_millisecond(now)
    if started_at is None or current is None or started_at > current:
        return None
    return floor_utc_millisecond(started_at + timedelta(seconds=ttl_seconds))


def intent_age_expired(intent: object, *, now: datetime) -> bool:
    """Return true only when a durable UTC creation time is over 24 hours old."""
    created_at = getattr(intent, "created_at", None)
    if (
        not isinstance(created_at, datetime)
        or created_at.tzinfo is None
        or created_at.utcoffset() != timedelta(0)
        or now.tzinfo is None
        or now.utcoffset() != timedelta(0)
    ):
        return False
    created_at = floor_utc_millisecond(created_at)
    current = floor_utc_millisecond(now)
    if created_at is None or current is None or created_at > current:
        return False
    return is_historical(created_at, current)


def has_admission_origin(intent: object) -> bool:
    snapshot = getattr(intent, "strategy_snapshot", None)
    origin = snapshot.get("admission_origin") if isinstance(snapshot, dict) else None
    if not isinstance(origin, dict):
        return False
    try:
        UUID(origin["runtime_instance_id"])
    except (KeyError, TypeError, ValueError):
        return False
    generation = origin.get("generation")
    revision = origin.get("revision")
    fingerprint = origin.get("configuration_fingerprint")
    return (
        type(generation) is int
        and generation > 0
        and type(revision) is int
        and revision > 0
        and isinstance(fingerprint, str)
        and len(fingerprint) == 64
        and all(character in "0123456789abcdef" for character in fingerprint)
    )
