"""Probe exposes only read RPCs and sanitized outcomes."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "prod_probe", Path(__file__).parents[1] / "develop/scripts/prod_readonly_probe.py"
)
assert SPEC
assert SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_missing_token_has_explicit_safe_status(tmp_path):
    assert probe.read_token(tmp_path / "missing") == (None, "TOKEN_MISSING")


def test_probe_calls_only_production_read_methods_and_masks_identifiers():
    calls = []
    account = "private-account-123"

    async def accounts():
        calls.append("users.get_accounts")
        return SimpleNamespace(accounts=[SimpleNamespace(id=account, status=2, access_level=2)])

    async def read(**arguments):
        assert arguments == {"account_id": account}
        calls.append("read")
        return SimpleNamespace(private_value="do-not-export", amounts=[12345])

    services = SimpleNamespace(
        users=SimpleNamespace(get_accounts=accounts),
        operations=SimpleNamespace(get_portfolio=read, get_positions=read),
        orders=SimpleNamespace(get_orders=read),
        stop_orders=SimpleNamespace(get_stop_orders=read),
    )

    class Client:
        async def __aenter__(self):
            return services

        async def __aexit__(self, *args):
            pass

    def factory(token, *, target):
        assert token == "synthetic"
        assert target == "invest-public-api.tbank.ru:443"
        return Client()

    result = asyncio.run(probe.probe_reads("synthetic", client_factory=factory))
    assert calls == ["users.get_accounts", "read", "read", "read", "read"]
    assert result["status"] == "PASS"
    assert account not in str(result)
    assert "12345" not in str(result)
    assert "do-not-export" not in str(result)


def test_rpc_error_is_sanitized(caplog):
    import logging  # noqa: PLC0415

    class Client:
        async def __aenter__(self):
            logging.getLogger("t_tech.invest.logging").error("token-and-account-private")
            raise RuntimeError("token-and-account-private")

        async def __aexit__(self, *args):
            pass

    result = asyncio.run(probe.probe_reads("synthetic", client_factory=lambda *args, **kwargs: Client()))
    assert result == {"status": "RPC_FAILED"}
    assert "token-and-account-private" not in caplog.text


def test_token_refuses_symlink_and_nonroot_or_public_file(tmp_path):
    import os  # noqa: PLC0415

    path = tmp_path / "token"
    path.write_text("synthetic")
    path.chmod(0o644)
    assert probe.read_token(path) == (None, "TOKEN_PERMISSIONS_INVALID")
    path.chmod(0o600)
    if os.geteuid() != 0:
        assert probe.read_token(path) == (None, "TOKEN_PERMISSIONS_INVALID")
    link = tmp_path / "link"
    link.symlink_to(path)
    assert probe.read_token(link) == (None, "TOKEN_UNAVAILABLE")


@pytest.mark.parametrize("access_level", [0, 1, 3])
def test_nonreadonly_credential_stops_before_financial_reads(access_level):
    async def accounts():
        return SimpleNamespace(accounts=[SimpleNamespace(id="private", status=2, access_level=access_level)])

    class Client:
        async def __aenter__(self):
            return SimpleNamespace(users=SimpleNamespace(get_accounts=accounts))

        async def __aexit__(self, *args):
            pass

    result = asyncio.run(probe.probe_reads("synthetic", client_factory=lambda *args, **kwargs: Client()))
    assert result == {"status": "TOKEN_NOT_READ_ONLY"}


def test_empty_accounts_do_not_claim_real_read_acceptance():
    async def accounts():
        return SimpleNamespace(accounts=[])

    class Client:
        async def __aenter__(self):
            return SimpleNamespace(users=SimpleNamespace(get_accounts=accounts))

        async def __aexit__(self, *args):
            pass

    assert asyncio.run(probe.probe_reads("synthetic", client_factory=lambda *args, **kwargs: Client())) == {
        "status": "NO_OPEN_ACCOUNTS"
    }
