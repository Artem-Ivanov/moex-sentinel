"""Operator authentication is the boundary of the browser API."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.api.auth import SESSION_SECONDS, OperatorAuth, hash_password, main, parse_password_hash


def client_for_auth(tmp_path: Path, monkeypatch, *, local: bool = True) -> TestClient:
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("a long test password"))
    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", str(local).lower())
    app = create_app(database_url=f"sqlite:///{tmp_path / 'auth.db'}")
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_anonymous_api_is_private_but_health_is_public(tmp_path: Path, monkeypatch) -> None:
    with client_for_auth(tmp_path, monkeypatch) as client:
        response = client.get("/api/brokers")
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/api/health").status_code == 200


def test_login_session_csrf_and_logout(tmp_path: Path, monkeypatch) -> None:
    with client_for_auth(tmp_path, monkeypatch) as client:
        bad = client.post("/api/auth/login", json={"username": "wrong", "password": "a long test password"})
        assert bad.status_code == 401
        assert "AUTH_PASSWORD_HASH" not in bad.text
        login = client.post("/api/auth/login", json={"username": "operator", "password": "a long test password"})
        assert login.status_code == 200
        assert "moex-local-session=" in login.headers["set-cookie"]
        assert "httponly" in login.headers["set-cookie"].lower()
        assert "samesite=strict" in login.headers["set-cookie"].lower()
        raw_cookie = client.cookies.get("moex-local-session")
        assert raw_cookie
        csrf = login.json()["csrf_token"]
        assert client.get("/api/auth/session").json()["username"] == "operator"
        assert client.post("/api/auth/logout").status_code == 403
        assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
        assert client.get("/api/auth/session").status_code == 401
        assert (
            client.get("/api/auth/session", headers={"Cookie": f"moex-local-session={raw_cookie}"}).status_code == 401
        )


def test_local_cookie_rejected_for_other_host(tmp_path: Path, monkeypatch) -> None:
    with client_for_auth(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "a long test password"},
            headers={"Host": "example.com"},
        )
        assert response.status_code == 403
        assert "set-cookie" not in response.headers


def test_missing_credentials_prevent_startup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "")
    with (
        pytest.raises(ValueError, match="AUTH_USERNAME"),
        TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'missing.db'}")),
    ):
        pass


def test_internal_contract_does_not_require_operator_cookie(tmp_path: Path, monkeypatch) -> None:
    with client_for_auth(tmp_path, monkeypatch) as client:
        assert client.post("/internal/automaton/heartbeats", json={}).status_code == 422


def test_secure_cookie_and_local_cookie_are_distinct(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("a long test password"))
    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "false")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://example.com")
    app = create_app(database_url=f"sqlite:///{tmp_path / 'secure.db'}")
    with TestClient(app, base_url="https://example.com", headers={"Origin": "https://example.com"}) as client:
        login = client.post("/api/auth/login", json={"username": "operator", "password": "a long test password"})
        assert login.status_code == 200
        assert "__Host-moex-session=" in login.headers["set-cookie"]
        assert "secure" in login.headers["set-cookie"].lower()
        client.cookies.set("moex-local-session", "fake")
        assert client.get("/api/auth/session").status_code == 200
        client.cookies.delete("__Host-moex-session")
        assert client.get("/api/auth/session").status_code == 401


def test_csrf_rejects_mutation_before_handler_and_session_expires(tmp_path: Path, monkeypatch) -> None:
    with client_for_auth(tmp_path, monkeypatch) as client:
        login = client.post("/api/auth/login", json={"username": "operator", "password": "a long test password"})
        assert login.status_code == 200
        assert client.post("/api/brokers", json={}).status_code == 403
        accepted = client.post("/api/brokers", json={}, headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert accepted.status_code == 422
        auth = client.app.state.operator_auth
        auth.clock = lambda: float("inf")
        assert client.get("/api/auth/session").status_code == 401


@pytest.mark.asyncio
async def test_scrypt_slots_return_busy_without_username_disclosure() -> None:
    auth = OperatorAuth("operator", hash_password("a long test password"))
    await auth.verification_slots.acquire()
    await auth.verification_slots.acquire()
    try:
        assert await auth.verify("operator", "a long test password") is None
        assert await auth.verify("wrong", "a long test password") is None
    finally:
        auth.verification_slots.release()
        auth.verification_slots.release()
    assert not await auth.verify("wrong", "a long test password")
    assert not await auth.verify("другой", "a long test password")
    assert SESSION_SECONDS == 43200
    assert parse_password_hash(hash_password("a long test password"))


@pytest.mark.asyncio
async def test_cancellation_does_not_free_scrypt_slot_while_thread_runs(monkeypatch) -> None:
    auth = OperatorAuth("operator", hash_password("a long test password"))
    started = threading.Event()
    release = threading.Event()

    def blocked_scrypt(*_args, **_kwargs) -> bytes:
        started.set()
        release.wait(timeout=5)
        return auth.digest

    monkeypatch.setattr("moex_sentinel.api.auth.hashlib.scrypt", blocked_scrypt)
    await auth.verification_slots.acquire()
    task = asyncio.create_task(auth.verify("operator", "a long test password"))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert auth.verification_slots.locked()
    finally:
        release.set()
        auth.verification_slots.release()


def test_hash_cli_accepts_unicode_password_without_echo(monkeypatch, capsys) -> None:
    password = "пароль для оператора 12345"
    monkeypatch.setattr("sys.argv", ["auth", "hash-password"])
    monkeypatch.setattr("moex_sentinel.api.auth.getpass.getpass", lambda _prompt: password)
    main()
    output = capsys.readouterr().out
    assert output.startswith("scrypt:16384:8:5:")
    assert password not in output


def test_busy_login_returns_429_while_health_stays_available(tmp_path: Path, monkeypatch) -> None:
    with client_for_auth(tmp_path, monkeypatch) as client:
        auth = client.app.state.operator_auth
        started = threading.Event()
        release = threading.Event()
        started_count = 0
        guard = threading.Lock()

        def blocked_scrypt(*_args, **_kwargs) -> bytes:
            nonlocal started_count
            with guard:
                started_count += 1
                if started_count == 2:
                    started.set()
            release.wait(timeout=5)
            return auth.digest

        monkeypatch.setattr("moex_sentinel.api.auth.hashlib.scrypt", blocked_scrypt)
        body = {"username": "operator", "password": "a long test password"}
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(client.post, "/api/auth/login", json=body)
            second = executor.submit(client.post, "/api/auth/login", json=body)
            try:
                assert started.wait(timeout=5)
                busy = client.post("/api/auth/login", json=body)
                assert busy.status_code == 429
                assert busy.headers["Retry-After"] == "1"
                assert client.get("/api/health").status_code == 200
            finally:
                release.set()
            assert first.result().status_code == 200
            assert second.result().status_code == 200


def test_configured_cookie_and_origin_reject_cross_environment_before_logout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("a long test password"))
    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "false")
    monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-moex-prod-session")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://example.com:8443")
    app = create_app(database_url=f"sqlite:///{tmp_path / 'isolated.db'}")
    body = {"username": "operator", "password": "a long test password"}
    with TestClient(app, base_url="https://example.com:8443") as client:
        assert client.post("/api/auth/login", json=body).status_code == 403
        assert client.post("/api/auth/login", json=body, headers={"Origin": "https://example.com"}).status_code == 403
        login = client.post("/api/auth/login", json=body, headers={"Origin": "https://example.com:8443"})
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert cookie.startswith("__Host-moex-prod-session=")
        assert "Domain=" not in cookie
        assert "Path=/" in cookie
        csrf = login.json()["csrf_token"]
        client.cookies.set("__Host-moex-session", "other-environment-session")
        for origin in ("https://example.com", "https://example.com:8443/", "null"):
            assert client.post("/api/auth/logout", headers={"Origin": origin, "X-CSRF-Token": csrf}).status_code == 403
            assert client.get("/api/auth/session").status_code == 200
        assert (
            client.post(
                "/api/auth/logout", headers={"Origin": "https://example.com:8443", "X-CSRF-Token": csrf}
            ).status_code
            == 204
        )
        assert client.cookies.get("__Host-moex-session") == "other-environment-session"


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com:8443/",
        "https://example.com:8443/path",
        "https://user@example.com:8443",
        "https://example.com:8443?x=1",
    ],
)
def test_operator_auth_rejects_non_origin_configuration(origin: str) -> None:
    with pytest.raises(ValueError, match="AUTH_ALLOWED_ORIGIN"):
        OperatorAuth("operator", hash_password("a long test password"), allowed_origin=origin)


def test_two_contours_share_host_but_reject_each_others_origin_and_csrf(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("a long test password"))
    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "false")
    monkeypatch.setenv("BROKER_ACCESS_MODE", "READ_ONLY")
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "TEST")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://example.com")
    monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-moex-session")
    sandbox_app = create_app(database_url=f"sqlite:///{tmp_path / 'sandbox.db'}")
    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://example.com:8443")
    monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-moex-prod-session")
    prod_app = create_app(database_url=f"sqlite:///{tmp_path / 'prod.db'}")
    with (
        TestClient(sandbox_app, base_url="https://example.com") as sandbox,
        TestClient(prod_app, base_url="https://example.com:8443") as prod,
    ):
        clients = [
            (sandbox, "https://example.com", "__Host-moex-session"),
            (prod, "https://example.com:8443", "__Host-moex-prod-session"),
        ]
        csrf_tokens = []
        cookies = []
        for client, origin, name in clients:
            login = client.post(
                "/api/auth/login",
                json={"username": "operator", "password": "a long test password"},
                headers={"Origin": origin},
            )
            assert login.status_code == 200
            csrf_tokens.append(login.json()["csrf_token"])
            cookies.append(f"{name}={client.cookies.get(name)}")
            assert client.get("/api/runtime").json() == {
                "environment": "PROD" if client is prod else "TEST",
                "access_mode": "READ_ONLY",
            }
        for index, (client, origin, _) in enumerate(clients):
            for csrf in csrf_tokens:
                headers = {"Origin": clients[1 - index][1], "Cookie": "; ".join(cookies), "X-CSRF-Token": csrf}
                for path in ("/api/auth/logout", "/api/brokers", "/api/instruments/id/id/trade"):
                    response = client.post(path, json={}, headers=headers)
                    assert response.status_code == 403
                    assert response.json()["detail"] == "Invalid Origin"
            assert client.get("/api/auth/session").status_code == 200
            for path in (
                "/api/instruments/id/id/trade",
                "/api/trading-automations/id/resume",
                "/api/trading-automations/id/close",
            ):
                assert (
                    client.post(
                        path, json={}, headers={"Origin": origin, "X-CSRF-Token": csrf_tokens[index]}
                    ).status_code
                    == 403
                )
