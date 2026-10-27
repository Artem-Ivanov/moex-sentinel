import asyncio
import json
import logging
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import ProxyHandler

import pytest
from pydantic import SecretStr

from moex_sentinel.adapters import telegram_alerts
from moex_sentinel.adapters.telegram_alerts import (
    TelegramDeliveryError,
    create_telegram_alert_transport,
)


class Response(BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def make_transport(monkeypatch, response, *, request_timeout_seconds=telegram_alerts.REQUEST_TIMEOUT_SECONDS):
    captured = {}

    class Opener:
        def open(self, request_value, timeout):
            captured["request"] = request_value
            captured["timeout"] = timeout
            return response

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(telegram_alerts, "build_opener", build_opener)

    async def run_sync(function):
        return function()

    return (
        create_telegram_alert_transport(
            bot_token=SecretStr("12345:secret_token"),
            chat_id="safe-chat",
            run_sync=run_sync,
            request_timeout_seconds=request_timeout_seconds,
        ),
        captured,
    )


def test_sender_posts_strict_success_json_through_fixed_https_without_proxy_or_redirect(monkeypatch):
    transport, captured = make_transport(monkeypatch, Response(b'{"ok":true,"result":{}}'))

    asyncio.run(transport.send_message("SAFE_STATUS"))

    request_value = captured["request"]
    assert request_value.full_url == "https://api.telegram.org/bot12345:secret_token/sendMessage"
    assert json.loads(request_value.data) == {"chat_id": "safe-chat", "text": "SAFE_STATUS"}
    assert captured["timeout"] <= 5
    proxy_handlers = [handler for handler in captured["handlers"] if isinstance(handler, ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert any(handler.__class__.__name__ == "_NoRedirectHandler" for handler in captured["handlers"])


def test_sender_uses_configured_request_timeout(monkeypatch):
    transport, captured = make_transport(
        monkeypatch, Response(b'{"ok":true,"result":{}}'), request_timeout_seconds=2.25
    )

    asyncio.run(transport.send_message("SAFE_STATUS"))

    assert captured["timeout"] == 2.25


@pytest.mark.parametrize(
    "body",
    [
        b'{"ok":false,"description":"12345:secret_token"}',
        b'{"ok":"true","result":{}}',
        b"not-json",
        b'{"ok":true}',
    ],
)
def test_sender_rejects_telegram_errors_malformed_json_and_success_without_result(monkeypatch, body, caplog):
    transport, _ = make_transport(monkeypatch, Response(body))

    with caplog.at_level(logging.ERROR), pytest.raises(TelegramDeliveryError):
        asyncio.run(transport.send_message("SAFE_STATUS"))

    assert "12345:secret_token" not in caplog.text
    assert "description" not in caplog.text
    assert "12345:secret_token" not in str(TelegramDeliveryError("HTTP_ERROR"))


def test_sender_rejects_oversized_response_before_json_parsing(monkeypatch):
    class OversizedResponse(Response):
        def read(self, size=-1):
            assert size <= 65_537
            return b"x" * 65_537

    transport, _ = make_transport(monkeypatch, OversizedResponse(b""))

    with pytest.raises(TelegramDeliveryError) as error:
        asyncio.run(transport.send_message("SAFE_STATUS"))

    assert error.value.code == "RESPONSE_TOO_LARGE"


def test_sender_rejects_redirect_without_exposing_request_url(monkeypatch, caplog):
    class RedirectingOpener:
        def open(self, request_value, timeout):
            raise HTTPError(request_value.full_url, 302, "Found", {}, None)

    monkeypatch.setattr(telegram_alerts, "build_opener", lambda *_: RedirectingOpener())

    async def run_sync(function):
        return function()

    transport = create_telegram_alert_transport(
        bot_token=SecretStr("12345:secret_token"), chat_id="safe-chat", run_sync=run_sync
    )

    with caplog.at_level(logging.ERROR), pytest.raises(TelegramDeliveryError) as error:
        asyncio.run(transport.send_message("SAFE_STATUS"))

    assert error.value.code == "HTTP_ERROR"
    assert "12345:secret_token" not in str(error.value)
    assert "12345:secret_token" not in caplog.text


def test_sender_sanitizes_opener_construction_failures(monkeypatch):
    monkeypatch.setattr(
        telegram_alerts,
        "build_opener",
        lambda *_: (_ for _ in ()).throw(ValueError("credential-bearing-url")),
    )

    async def run_sync(function):
        return function()

    transport = create_telegram_alert_transport(
        bot_token=SecretStr("12345:secret_token"), chat_id="safe-chat", run_sync=run_sync
    )

    with pytest.raises(TelegramDeliveryError) as error:
        asyncio.run(transport.send_message("SAFE_STATUS"))

    assert error.value.code == "NETWORK_ERROR"
    assert "credential-bearing-url" not in str(error.value)


@pytest.mark.parametrize("token", ["12345:abc/evil", "12345:abc?secret", "12345:abc\nforged"])
def test_sender_rejects_malformed_token_without_executor_or_network(token):
    runner_calls = []

    async def run_sync(function):
        runner_calls.append(function)
        return function()

    transport = create_telegram_alert_transport(bot_token=SecretStr(token), chat_id="safe-chat", run_sync=run_sync)

    with pytest.raises(TelegramDeliveryError) as error:
        asyncio.run(transport.send_message("SAFE_STATUS"))

    assert error.value.code == "INVALID_RESPONSE"
    assert token not in str(error.value)
    assert runner_calls == []
