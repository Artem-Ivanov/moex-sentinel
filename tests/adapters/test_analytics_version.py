"""Offline private health adapter: safe errors, deadline and streaming bounds."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from moex_sentinel.adapters.analytics_version import AnalyticsVersionClient


async def observation(response=None, *, url="http://analytics:8001", handler=None):
    requests = []

    def handle(request):
        requests.append(request)
        return handler(request) if handler else response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
    ) as http:
        result = await AnalyticsVersionClient(url, http, clock=lambda: datetime(2026, 10, 3, tzinfo=UTC)).observe()
    return result, requests


def test_observes_actual_health_version_without_credentials():
    result, requests = asyncio.run(
        observation(httpx.Response(200, json={"status": "ok", "service": "analytics", "version": "4.5.6"}))
    )
    assert result.version == "4.5.6"
    assert result.age_ms == 0
    assert result.received_at == "2026-10-03T00:00:00.000Z"
    assert str(requests[0].url) == "http://analytics:8001/health"
    assert "authorization" not in requests[0].headers
    assert "cookie" not in requests[0].headers


def test_unconfigured_does_not_make_network_request():
    result, requests = asyncio.run(observation(url=""))
    assert result.reason == "NOT_CONFIGURED"
    assert requests == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://secret/"}),
        httpx.Response(500, text="private-error"),
        httpx.Response(200, text="bad-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"status": "ok", "service": "core", "version": "4.5.6"}),
        httpx.Response(200, json={"status": "ok", "service": "analytics", "version": "secret"}),
        httpx.Response(200, content=b" " * 4097),
    ],
)
def test_invalid_responses_are_safe_unknown_without_redirect(response):
    result, requests = asyncio.run(observation(response))
    assert result.version is None
    assert result.reason == "INVALID_RESPONSE"
    assert len(requests) == 1
    assert "secret" not in result.model_dump_json()
    assert "private-error" not in result.model_dump_json()


class DelayedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"{"
        await asyncio.sleep(1.1)
        yield b"}"

    async def aclose(self):
        self.closed = True


def test_total_deadline_includes_body_and_closes_stream():
    stream = DelayedBody()
    result, _ = asyncio.run(observation(httpx.Response(200, stream=stream)))
    assert result.reason == "UNAVAILABLE"
    assert stream.closed


class OversizeBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b" " * 4096
        yield b"!"
        pytest.fail("must stop reading oversized response")

    async def aclose(self):
        self.closed = True


def test_stream_oversize_stops_before_remaining_body_and_closes():
    stream = OversizeBody()
    result, _ = asyncio.run(observation(httpx.Response(200, stream=stream)))
    assert result.reason == "INVALID_RESPONSE"
    assert stream.closed


def test_connection_error_does_not_leak_request_or_secret():
    def failed(request):
        raise httpx.ConnectError("secret-url-password", request=request)

    result, _ = asyncio.run(observation(handler=failed))
    assert result.reason == "UNAVAILABLE"
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize("url", ["http://analytics:invalid", "http://user:password@analytics", "ftp://analytics"])
def test_invalid_configured_url_is_safe_unknown_without_external_request(url):
    result, requests = asyncio.run(observation(url=url))
    assert result.reason == "INVALID_RESPONSE"
    assert requests == []
