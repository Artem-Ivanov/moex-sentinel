"""Private Analytics health observation, with bounded time and body size."""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from sentinel_contracts.runtime_versions import SafeVersion, VersionObservation
from sentinel_contracts.time import floor_utc_millisecond, utc_now_ms

_VERSION = TypeAdapter(SafeVersion)


class AnalyticsVersionClient:
    def __init__(self, url: str, http: httpx.AsyncClient, *, clock: Callable[[], datetime] = utc_now_ms) -> None:
        self._url = url.rstrip("/")
        self._http = http
        self._clock = clock

    async def observe(self) -> VersionObservation:
        if not self._url:
            return VersionObservation(reason="NOT_CONFIGURED")
        try:
            target = urlsplit(self._url)
            if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
                return VersionObservation(reason="INVALID_RESPONSE")
            async with asyncio.timeout(1.0):
                async with self._http.stream(
                    "GET", self._url + "/health", follow_redirects=False, timeout=1.0
                ) as response:
                    if response.status_code != 200:
                        return VersionObservation(reason="INVALID_RESPONSE")
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=4097):
                        if len(content) + len(chunk) > 4096:
                            return VersionObservation(reason="INVALID_RESPONSE")
                        content.extend(chunk)
                    payload = json.loads(content)
                    if (
                        not isinstance(payload, dict)
                        or payload.get("status") != "ok"
                        or payload.get("service") != "analytics"
                    ):
                        return VersionObservation(reason="INVALID_RESPONSE")
                    version = _VERSION.validate_python(payload.get("version"))
            timestamp = floor_utc_millisecond(self._clock()).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            return VersionObservation(
                observation="OBSERVED", version=version, received_at=timestamp, age_ms=0, reason="OBSERVED"
            )
        except (httpx.HTTPError, TimeoutError):
            return VersionObservation(reason="UNAVAILABLE")
        except (ValueError, ValidationError, httpx.InvalidURL):
            return VersionObservation(reason="INVALID_RESPONSE")
