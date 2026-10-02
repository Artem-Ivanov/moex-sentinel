"""Single-operator credentials and short-lived in-memory browser sessions."""

import asyncio
import getpass
import hashlib
import hmac
import re
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request

SESSION_SECONDS = 12 * 60 * 60
SECURE_COOKIE = "__Host-moex-session"
LOCAL_COOKIE = "moex-local-session"
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 5
_MAX_PASSWORD_BYTES = 1024


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if not 16 <= len(encoded) <= _MAX_PASSWORD_BYTES:
        raise ValueError("Password must contain 16–1024 UTF-8 bytes")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(encoded, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}:{salt.hex()}:{digest.hex()}"


def parse_password_hash(value: str) -> tuple[bytes, bytes]:
    if not re.fullmatch(r"scrypt:16384:8:5:[0-9a-fA-F]{32}:[0-9a-fA-F]{64}", value):
        raise ValueError("AUTH_PASSWORD_HASH has invalid format")
    _, _, _, _, salt_hex, digest_hex = value.split(":")
    return bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)


@dataclass(frozen=True)
class Session:
    username: str
    csrf_token: str
    expires_at: float


class OperatorAuth:
    def __init__(
        self,
        username: str,
        password_hash: str,
        *,
        insecure_loopback: bool = False,
        session_cookie_name: str = SECURE_COOKIE,
        allowed_origin: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= len(username) <= 64 or "\n" in username or "\r" in username:
            raise ValueError("AUTH_USERNAME has invalid format")
        self.username = username
        self.salt, self.digest = parse_password_hash(password_hash)
        if not re.fullmatch(r"__Host-[A-Za-z0-9_-]+", session_cookie_name):
            raise ValueError("AUTH_SESSION_COOKIE_NAME must be a safe __Host- cookie name")
        self.session_cookie_name = session_cookie_name
        self.allowed_origin = self.parse_origin(allowed_origin) if allowed_origin else None
        if self.allowed_origin is not None and not insecure_loopback and self.allowed_origin[0] != "https":
            raise ValueError("AUTH_ALLOWED_ORIGIN requires HTTPS")
        self.insecure_loopback = insecure_loopback
        self.clock = clock
        self.sessions: dict[str, Session] = {}
        self.verification_slots = asyncio.Semaphore(2)

    def cookie_name(self, request: Request) -> str | None:
        if not self.insecure_loopback:
            return self.session_cookie_name
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        return LOCAL_COOKIE if host in {"127.0.0.1", "localhost"} else None

    @staticmethod
    def parse_origin(value: str) -> tuple[str, str, int]:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or any(ord(character) <= 32 or ord(character) == 127 for character in value)
                or parsed.netloc.endswith(":")
                or value.strip() != value
                or "?" in value
                or "#" in value
            ):
                raise ValueError  # noqa: TRY301 - normalize URL parser and shape failures together.
            return (
                parsed.scheme,
                parsed.hostname.lower(),
                parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80),
            )
        except ValueError as error:
            raise ValueError("AUTH_ALLOWED_ORIGIN must be an exact scheme/host/port origin") from error

    def origin_allowed(self, request: Request) -> bool:
        if self.allowed_origin is None:
            return self.insecure_loopback and self.cookie_name(request) is not None
        try:
            return self.parse_origin(request.headers.get("origin", "")) == self.allowed_origin
        except ValueError:
            return False

    async def verify(self, username: str, password: str) -> bool | None:
        encoded = password.encode("utf-8")
        if len(username) > 64 or not 1 <= len(encoded) <= _MAX_PASSWORD_BYTES:
            return False
        if self.verification_slots.locked():
            return None
        await self.verification_slots.acquire()
        work = asyncio.create_task(
            asyncio.to_thread(hashlib.scrypt, encoded, salt=self.salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
        )

        def release_slot(done: asyncio.Task[bytes]) -> None:
            self.verification_slots.release()
            if not done.cancelled():
                done.exception()

        work.add_done_callback(release_slot)
        candidate = await asyncio.shield(work)
        return hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8")) & hmac.compare_digest(
            candidate, self.digest
        )

    def create_session(self) -> tuple[str, Session]:
        self._remove_expired()
        if len(self.sessions) >= 128:
            oldest = min(self.sessions, key=lambda key: self.sessions[key].expires_at)
            del self.sessions[oldest]
        raw_id = secrets.token_urlsafe(32)
        session = Session(self.username, secrets.token_urlsafe(32), self.clock() + SESSION_SECONDS)
        self.sessions[self._session_key(raw_id)] = session
        return raw_id, session

    def get_session(self, raw_id: str | None) -> Session | None:
        if not raw_id or len(raw_id) != 43:
            return None
        key = self._session_key(raw_id)
        session = self.sessions.get(key)
        if session is not None and session.expires_at <= self.clock():
            del self.sessions[key]
            return None
        return session

    def revoke(self, raw_id: str | None) -> None:
        if raw_id:
            self.sessions.pop(self._session_key(raw_id), None)

    def _remove_expired(self) -> None:
        now = self.clock()
        for key, session in list(self.sessions.items()):
            if session.expires_at <= now:
                del self.sessions[key]

    @staticmethod
    def _session_key(raw_id: str) -> str:
        return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()


def main() -> None:
    if sys.argv[1:] != ["hash-password"]:
        raise SystemExit("Usage: python -m moex_sentinel.api.auth hash-password")
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if not hmac.compare_digest(password.encode("utf-8"), confirmation.encode("utf-8")):
        raise SystemExit("Passwords do not match")
    try:
        print(hash_password(password))  # noqa: T201 - requested CLI output is only the verifier.
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
