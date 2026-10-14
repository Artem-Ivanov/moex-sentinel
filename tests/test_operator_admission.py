"""The operator CLI keeps admission changes behind the existing browser auth contract."""

import json
import stat
from datetime import date, datetime, timezone
from decimal import Decimal
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from sentinel_contracts.trading_admission import (
    AdmissionBaseline,
    AdmissionLimits,
    AdmissionStatus,
    AdmissionView,
    ArmRequest,
    CaptureBaselineRequest,
    OwnerStoppedRequest,
    ReconciliationRequest,
    ReconciliationResult,
    ReconciliationStatus,
    RevokeRequest,
    SubmitDenialReason,
)

BROKER_ID = UUID("00000000-0000-4000-8000-000000000101")
NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
ORIGIN = "https://operator.example.test:8443"
BASE_URL = ORIGIN + "/"
PASSWORD = "private operator password"
CSRF = "c" * 43


def test_operator_admission_cli_module_exists():
    assert find_spec("moex_sentinel.operator_admission") is not None


def cli_module():
    return import_module("moex_sentinel.operator_admission")


def baseline() -> AdmissionBaseline:
    return AdmissionBaseline(
        baseline_id=UUID("00000000-0000-4000-8000-000000000201"),
        broker_id=BROKER_ID,
        account_id="prod-account",
        moscow_day=date(2026, 10, 9),
        timezone="Europe/Moscow",
        currency="RUB",
        equity=Decimal("100000"),
        net_external_cash_flow=Decimal("0"),
        capture_started_at=NOW,
        capture_finished_at=NOW,
        source="GET_PORTFOLIO",
        operations_through=NOW,
        accepted_by="operator",
        accepted_at=NOW,
        policy="FIRST_ACCEPTED_ACCOUNT_EQUITY",
    )


def reconciliation() -> ReconciliationResult:
    return ReconciliationResult(
        reconciliation_id=UUID("00000000-0000-4000-8000-000000000301"),
        broker_id=BROKER_ID,
        account_id="prod-account",
        scope="ACCOUNT",
        owner_runtime_instance_id=UUID("00000000-0000-4000-8000-000000000302"),
        owner_generation=1,
        configuration_fingerprint="a" * 64,
        status=ReconciliationStatus.ACCOUNT_ONLY,
        canary_uid=None,
        captured_at=NOW,
        valid_until=NOW,
        can_arm=False,
        position_anchor_units=None,
        active_ordinary_order_count=0,
        active_stop_order_count=0,
        unresolved_reservation_count=0,
        reasons=(SubmitDenialReason.RECONCILIATION_REQUIRED,),
        risk_snapshot_id=None,
    )


def admission_view() -> AdmissionView:
    return AdmissionView(
        broker_id=BROKER_ID,
        account_id="prod-account",
        revision=1,
        owner_runtime_instance_id=None,
        owner_generation=None,
        recovery_runtime_instance_id=None,
        admission_status=AdmissionStatus.DISARMED,
        arm_revision=None,
        lease_expires_at=None,
        arm_expires_at=None,
        limits=None,
        configuration_fingerprint=None,
        baseline=None,
        reconciliation=None,
        effective_can_submit=False,
        effective_denial_reasons=(SubmitDenialReason.DISARMED,),
        unresolved_reservation_count=0,
        outstanding_order_count=0,
        recovery_only=False,
    )


def request_for(command: str):
    if command == "baseline":
        return CaptureBaselineRequest(day=date(2026, 10, 9), accept_policy=True)
    if command == "reconcile":
        return ReconciliationRequest(expected_revision=1)
    if command == "arm":
        return ArmRequest(
            expected_revision=1,
            runtime_instance_id=UUID("00000000-0000-4000-8000-000000000401"),
            baseline_id=UUID("00000000-0000-4000-8000-000000000402"),
            reconciliation_id=UUID("00000000-0000-4000-8000-000000000403"),
            limits=AdmissionLimits(
                canary_uid="TCS@TQBR",
                currency="RUB",
                max_order_notional=Decimal("1000"),
                exposure_scope="CANARY_UID_GROSS",
                max_exposure=Decimal("5000"),
                max_outstanding_orders=1,
                max_daily_loss=Decimal("500"),
            ),
            configuration_fingerprint="b" * 64,
            expires_at=NOW,
        )
    if command == "revoke":
        return RevokeRequest(expected_revision=1, reason="operator stop")
    return OwnerStoppedRequest(
        expected_revision=1,
        old_runtime_instance_id=UUID("00000000-0000-4000-8000-000000000501"),
        old_generation=1,
        candidate_runtime_instance_id=UUID("00000000-0000-4000-8000-000000000502"),
        stopped_at=NOW,
        auto_restart_disabled=True,
        evidence_ref="runbook/stop-1",
    )


def response_for(command: str):
    if command == "baseline":
        return baseline()
    if command == "reconcile":
        return reconciliation()
    return admission_view()


def write_request(tmp_path: Path, request) -> Path:
    path = tmp_path / "request.json"
    path.write_text(request.model_dump_json(), encoding="utf-8")
    return path


def argv(tmp_path: Path, command: str, *, password_file: Path | None = None, request=None) -> list[str]:
    result = ["--base-url", BASE_URL, "--username", "operator"]
    if password_file is not None:
        result.extend(("--password-file", str(password_file)))
    result.extend((str(BROKER_ID), command))
    if command != "view":
        result.extend(("--request-file", str(write_request(tmp_path, request or request_for(command)))))
    return result


def auth_transport(handler):
    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            assert request.headers["origin"] == ORIGIN
            assert json.loads(request.content) == {"username": "operator", "password": PASSWORD}
            return httpx.Response(
                200,
                json={"username": "operator", "csrf_token": CSRF},
                headers={"set-cookie": "__Host-moex-prod-session=session-value; Path=/; Secure; HttpOnly"},
            )
        return handler(request)

    return httpx.MockTransport(dispatch)


def test_view_uses_operator_cookie_origin_and_prints_only_admission_view(tmp_path, capsys):
    base = "/api/brokers/00000000-0000-4000-8000-000000000101/trading-admission"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == base
        assert request.headers["origin"] == ORIGIN
        assert "session-value" in request.headers["cookie"]
        return httpx.Response(200, json=admission_view().model_dump(mode="json"))

    status = cli_module().main(
        argv(tmp_path, "view"), transport=auth_transport(handler), password_prompt=lambda _: PASSWORD
    )

    output = capsys.readouterr()
    assert status == 0
    assert '"admission_status": "DISARMED"' in output.out
    assert PASSWORD not in output.out
    assert CSRF not in output.out
    assert "session-value" not in output.out
    assert output.err == ""


@pytest.mark.parametrize("command", ["baseline", "reconcile", "arm", "revoke", "owner-stopped"])
def test_mutations_validate_typed_json_and_send_cookie_origin_csrf(tmp_path, capsys, command):
    base = f"/api/brokers/{BROKER_ID}/trading-admission"
    path = f"{base}/{'baselines' if command == 'baseline' else command}"
    request_model = request_for(command)
    expected_response = response_for(command)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == path
        assert request.headers["origin"] == ORIGIN
        assert request.headers["x-csrf-token"] == CSRF
        assert "session-value" in request.headers["cookie"]
        assert json.loads(request.content) == request_model.model_dump(mode="json")
        return httpx.Response(200, json=expected_response.model_dump(mode="json"))

    status = cli_module().main(
        argv(tmp_path, command), transport=auth_transport(handler), password_prompt=lambda _: PASSWORD
    )

    output = capsys.readouterr()
    assert status == 0
    response_key = {"baseline": "baseline_id", "reconcile": "reconciliation_id"}.get(command, "admission_status")
    assert f'"{response_key}"' in output.out
    assert PASSWORD not in output.out
    assert CSRF not in output.out


@pytest.mark.parametrize(
    "body",
    ['{"expected_revision":1}', '{"expected_revision":1,"reason":"stop","max_daily_loss":999}'],
)
def test_invalid_request_is_rejected_before_any_network_request(tmp_path, capsys, body):
    request_path = tmp_path / "request.json"
    request_path.write_text(body, encoding="utf-8")

    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid request must not contact the operator API")

    args = ["--base-url", BASE_URL, str(BROKER_ID), "arm", "--request-file", str(request_path)]
    status = cli_module().main(args, transport=httpx.MockTransport(forbidden), password_prompt=lambda _: PASSWORD)

    output = capsys.readouterr()
    assert status != 0
    assert "max_daily_loss" not in output.err
    assert PASSWORD not in output.err


def test_password_file_requires_exact_mode_0600_before_network(tmp_path, capsys):
    password_file = tmp_path / "password"
    password_file.write_text(PASSWORD, encoding="utf-8")
    password_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)

    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("unsafe password file must not contact the operator API")

    status = cli_module().main(
        argv(tmp_path, "view", password_file=password_file),
        transport=httpx.MockTransport(forbidden),
    )

    output = capsys.readouterr()
    assert status != 0
    assert PASSWORD not in output.err


def test_password_file_mode_0600_authenticates_without_prompt(tmp_path, capsys):
    password_file = tmp_path / "password"
    password_file.write_text(PASSWORD + "\n", encoding="utf-8")
    password_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=admission_view().model_dump(mode="json"))

    status = cli_module().main(
        argv(tmp_path, "view", password_file=password_file),
        transport=auth_transport(handler),
        password_prompt=lambda _: pytest.fail("password prompt should not run"),
    )

    output = capsys.readouterr()
    assert status == 0
    assert PASSWORD not in output.out


@pytest.mark.parametrize("ca_file", [None, "custom-ca.pem"])
def test_tls_verification_stays_enabled_by_default_and_with_ca(tmp_path, monkeypatch, capsys, ca_file):
    module = cli_module()
    original_client = module.httpx.Client
    observed = {}

    def recording_client(*args, **kwargs):
        observed["verify"] = kwargs["verify"]
        return original_client(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "Client", recording_client)
    options = []
    if ca_file is not None:
        ca_path = tmp_path / ca_file
        ca_path.write_text("fixture CA", encoding="utf-8")
        options.extend(("--ca-file", str(ca_path)))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=admission_view().model_dump(mode="json"))

    status = module.main(
        ["--base-url", BASE_URL, *options, str(BROKER_ID), "view"],
        transport=auth_transport(handler),
        password_prompt=lambda _: PASSWORD,
    )

    capsys.readouterr()
    assert status == 0
    assert observed["verify"] is True if ca_file is None else observed["verify"] == str(ca_path)


def test_http_errors_do_not_echo_response_body_or_credentials(tmp_path, capsys):
    error_body = f"private broker token {PASSWORD}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=error_body)

    status = cli_module().main(
        argv(tmp_path, "view"), transport=auth_transport(handler), password_prompt=lambda _: PASSWORD
    )

    output = capsys.readouterr()
    assert status != 0
    assert "500" in output.err
    assert error_body not in output.err
    assert PASSWORD not in output.err


def test_unexpected_secret_in_success_response_is_rejected_and_not_printed(tmp_path, capsys):
    secret = "a" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=admission_view().model_dump(mode="json") | {"INTERNAL_WORKER_TOKEN": secret})

    status = cli_module().main(
        argv(tmp_path, "view"), transport=auth_transport(handler), password_prompt=lambda _: PASSWORD
    )

    output = capsys.readouterr()
    assert status != 0
    assert secret not in output.out
    assert secret not in output.err
    assert "invalid response" in output.err


def test_http_base_url_is_rejected_without_prompt_or_network(tmp_path, capsys):
    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("non-HTTPS origin must not contact the operator API")

    status = cli_module().main(
        ["--base-url", "http://operator.example.test", str(BROKER_ID), "view"],
        transport=httpx.MockTransport(forbidden),
        password_prompt=lambda _: pytest.fail("non-HTTPS origin must be rejected before password prompt"),
    )

    output = capsys.readouterr()
    assert status != 0
    assert "HTTPS" in output.err


def test_password_is_never_a_command_line_option_or_error_output(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli_module().main(["--base-url", BASE_URL, "--password", PASSWORD, str(BROKER_ID), "view"])
    output = capsys.readouterr()
    assert PASSWORD not in output.out
    assert PASSWORD not in output.err
