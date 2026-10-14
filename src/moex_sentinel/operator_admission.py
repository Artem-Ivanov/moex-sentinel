"""Small HTTPS CLI for operator-managed trading admission."""

from __future__ import annotations

import argparse
import getpass
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sentinel_contracts.trading_admission import (
    AdmissionBaseline,
    AdmissionView,
    ArmRequest,
    CaptureBaselineRequest,
    OwnerStoppedRequest,
    ReconciliationRequest,
    ReconciliationResult,
    RevokeRequest,
)

REQUEST_MODELS = {
    "baseline": CaptureBaselineRequest,
    "reconcile": ReconciliationRequest,
    "arm": ArmRequest,
    "revoke": RevokeRequest,
    "owner-stopped": OwnerStoppedRequest,
}
RESPONSE_MODELS = {
    "view": AdmissionView,
    "baseline": AdmissionBaseline,
    "reconcile": ReconciliationResult,
    "arm": AdmissionView,
    "revoke": AdmissionView,
    "owner-stopped": AdmissionView,
}


class CliError(Exception):
    """An error safe to show without including inputs or response bodies."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print("ERROR: invalid command line arguments.", file=sys.stderr)  # noqa: T201
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Inspect or change PROD trading admission through the Core API.", allow_abbrev=False
    )
    parser.add_argument("--base-url", required=True, help="HTTPS Core origin, without an API path")
    parser.add_argument("--username", default="operator")
    parser.add_argument("--password-file", type=Path, help="Password file with mode 0600")
    parser.add_argument("--ca-file", type=Path, help="Additional CA certificate for the production server")
    parser.add_argument("broker_id", type=UUID)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("view", help="Show current admission state")
    for name in REQUEST_MODELS:
        command = commands.add_parser(name)
        command.add_argument("--request-file", required=True, type=Path, help="JSON matching the typed request model")
    return parser


def _origin_and_base_url(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Validate malformed ports before constructing the client.
    except ValueError:
        raise CliError("Base URL must be an HTTPS origin without credentials or a path.") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise CliError("Base URL must be an HTTPS origin without credentials or a path.")
    origin = f"https://{parsed.netloc}"
    return origin, origin


def _read_request(command: str, path: Path):
    try:
        body = path.read_text(encoding="utf-8")
        return REQUEST_MODELS[command].model_validate_json(body)
    except (OSError, UnicodeError, ValidationError, ValueError):
        raise CliError(f"Request file does not match the {command} request contract.") from None


def _read_password(path: Path | None, prompt: Callable[[str], str]) -> str:
    if path is None:
        password = prompt("Operator password: ")
    else:
        try:
            if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise CliError("Password file must be a regular file with mode 0600.")
            password = path.read_text(encoding="utf-8").removesuffix("\n").removesuffix("\r")
        except (OSError, UnicodeError):
            raise CliError("Unable to read password file.") from None
    if not password or len(password.encode("utf-8")) > 1024:
        raise CliError("Operator password must contain 1–1024 UTF-8 bytes.")
    return password


def _api_path(broker_id: UUID, command: str) -> str:
    base = f"/api/brokers/{broker_id}/trading-admission"
    return base if command == "view" else f"{base}/{'baselines' if command == 'baseline' else command}"


def _json_response(response: httpx.Response, model, *, step: str):
    if response.status_code < 200 or response.status_code >= 300:
        raise CliError(f"{step} failed with HTTP {response.status_code}.")
    try:
        return model.model_validate(response.json())
    except (ValueError, ValidationError):
        raise CliError(f"{step} returned an invalid response.") from None


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    password_prompt: Callable[[str], str] = getpass.getpass,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        origin, base_url = _origin_and_base_url(args.base_url)
        request = _read_request(args.command, args.request_file) if args.command != "view" else None
        ca_file_is_valid = args.ca_file is None or args.ca_file.is_file()
        if not ca_file_is_valid:
            print("ERROR: CA file must be a readable certificate file.", file=sys.stderr)  # noqa: T201
            return 2
        password = _read_password(args.password_file, password_prompt)
        verify: bool | str = str(args.ca_file) if args.ca_file else True

        with httpx.Client(
            base_url=base_url,
            verify=verify,
            follow_redirects=False,
            trust_env=False,
            timeout=10.0,
            transport=transport,
        ) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": args.username, "password": password},
                headers={"Origin": origin},
            )
            login_data = _json_response(login, _LoginResponse, step="Operator login")
            headers = {"Origin": origin}
            if request is not None:
                headers["X-CSRF-Token"] = login_data.csrf_token
                response = client.post(
                    _api_path(args.broker_id, args.command),
                    json=request.model_dump(mode="json"),
                    headers=headers,
                )
            else:
                response = client.get(_api_path(args.broker_id, args.command), headers=headers)
            result = _json_response(response, RESPONSE_MODELS[args.command], step="Admission request")
        print(result.model_dump_json(indent=2))  # noqa: T201 - typed response models contain no credentials.
        return 0
    except CliError as error:
        print(f"ERROR: {error}", file=sys.stderr)  # noqa: T201 - messages contain no input or server body.
        return 2
    except (httpx.HTTPError, OSError, UnicodeError):
        print("ERROR: Operator API request failed.", file=sys.stderr)  # noqa: T201
        return 2


class _LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    username: str = Field(min_length=1, max_length=64)
    csrf_token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


if __name__ == "__main__":
    raise SystemExit(main())
