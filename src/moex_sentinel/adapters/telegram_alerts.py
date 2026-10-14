"""Minimal, secret-safe Telegram sender for readiness alerts."""

import asyncio
import json
import re
import ssl
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from pydantic import SecretStr

MAX_RESPONSE_BYTES = 64 * 1024
MAX_MESSAGE_LENGTH = 4096
REQUEST_TIMEOUT_SECONDS = 5
BOT_TOKEN_PATTERN = re.compile(r"[0-9]+:[A-Za-z0-9_-]+\Z")
DeliveryErrorCode = Literal["TIMEOUT", "HTTP_ERROR", "NETWORK_ERROR", "INVALID_RESPONSE", "RESPONSE_TOO_LARGE"]
T = TypeVar("T")
SyncRunner = Callable[[Callable[[], T]], Awaitable[T]]


class TelegramDeliveryError(Exception):
    """A failure category that never includes response or credential data."""

    def __init__(self, code: DeliveryErrorCode) -> None:
        self.code = code
        super().__init__(code)


class AlertTransport(Protocol):
    async def send_message(self, text: str) -> None: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class TelegramAlertTransport:
    def __init__(self, bot_token: SecretStr, chat_id: str, run_sync: SyncRunner) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._run_sync = run_sync

    async def send_message(self, text: str) -> None:
        if not text or len(text) > MAX_MESSAGE_LENGTH:
            raise TelegramDeliveryError("INVALID_RESPONSE")
        if not BOT_TOKEN_PATTERN.fullmatch(self._bot_token.get_secret_value()):
            raise TelegramDeliveryError("INVALID_RESPONSE")
        await self._run_sync(lambda: self._send(text))

    def _send(self, text: str) -> None:
        try:
            endpoint = f"https://api.telegram.org/bot{self._bot_token.get_secret_value()}/sendMessage"
            request = Request(  # noqa: S310 - endpoint is fixed HTTPS and redirects are rejected.
                endpoint,
                data=json.dumps({"chat_id": self._chat_id, "text": text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            opener = build_opener(
                ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), _NoRedirectHandler()
            )
            with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except TelegramDeliveryError:
            raise
        except HTTPError:
            raise TelegramDeliveryError("HTTP_ERROR") from None
        except (TimeoutError, asyncio.TimeoutError):
            raise TelegramDeliveryError("TIMEOUT") from None
        except URLError:
            raise TelegramDeliveryError("NETWORK_ERROR") from None
        except OSError:
            raise TelegramDeliveryError("NETWORK_ERROR") from None
        except Exception:
            raise TelegramDeliveryError("NETWORK_ERROR") from None

        if len(body) > MAX_RESPONSE_BYTES:
            raise TelegramDeliveryError("RESPONSE_TOO_LARGE")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramDeliveryError("INVALID_RESPONSE") from None
        if not isinstance(payload, dict) or payload.get("ok") is not True or "result" not in payload:
            raise TelegramDeliveryError("INVALID_RESPONSE")


def create_telegram_alert_transport(
    *, bot_token: SecretStr, chat_id: str, run_sync: SyncRunner
) -> TelegramAlertTransport:
    return TelegramAlertTransport(bot_token, chat_id, run_sync)
