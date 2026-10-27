"""Offline safety gates do not contact a daemon or broker."""

import importlib.util
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "production_preflight", Path(__file__).parents[1] / "deploy/remote/preflight.py"
)
assert spec
assert spec.loader
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def configuration():
    environment = {"APPLICATION_ENVIRONMENT": "PROD", "BROKER_ACCESS_MODE": "READ_ONLY"}
    credentials = {
        "INTERNAL_WORKER_ID": "production-worker",
        "INTERNAL_WORKER_BROKER_ID": "11111111-2222-4333-8444-555555555555",
        "INTERNAL_WORKER_TOKEN": "a" * 64,
    }
    return {
        "name": "moex-sentinel-prod",
        "networks": {
            "default": {"name": "moex-sentinel-prod_default"},
            "data": {"name": "moex-sentinel-data", "external": True},
        },
        "services": {
            "backend": {
                "environment": {
                    **environment,
                    **credentials,
                    "AUTH_SESSION_COOKIE_NAME": "__Host-moex-prod-session",
                    "AUTH_ALLOWED_ORIGIN": "https://135.136.178.252",
                    "DATABASE_URL": "postgresql+psycopg://sentinel:fixture@sentinel-shared-postgres:5432/moex_sentinel",
                }
            },
            "trading-automaton": {
                "environment": {
                    **environment,
                    **credentials,
                    "AUTOMATON_WORKER_ID": credentials["INTERNAL_WORKER_ID"],
                    "STRATEGY_ENABLED": "false",
                }
            },
            "frontend": {"ports": [{"host_ip": "127.0.0.1", "published": "8081", "target": 8080}]},
        },
        "volumes": {
            "automaton-data": {"external": True, "name": "moex-sentinel-prod-automaton-data"},
        },
    }


def test_prod_readonly_configuration_is_accepted():
    preflight.validate_contour_config(configuration())


def test_prod_trade_configuration_with_matching_credentials_is_accepted():
    config = configuration()
    for name in ("backend", "trading-automaton"):
        config["services"][name]["environment"]["BROKER_ACCESS_MODE"] = "TRADE"
    config["services"]["trading-automaton"]["environment"]["STRATEGY_ENABLED"] = "true"

    preflight.validate_contour_config(config)


def test_prod_trade_recovery_configuration_keeps_strategy_disabled():
    config = configuration()
    for name in ("backend", "trading-automaton"):
        config["services"][name]["environment"]["BROKER_ACCESS_MODE"] = "TRADE"

    preflight.validate_contour_config(config)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("INTERNAL_WORKER_ID", ""),
        ("INTERNAL_WORKER_ID", "replace-with-worker-id"),
        ("INTERNAL_WORKER_ID", "w" * 129),
        ("INTERNAL_WORKER_BROKER_ID", "not-a-uuid"),
        ("INTERNAL_WORKER_TOKEN", "replace-with-64-lowercase-hex-secret"),
        ("INTERNAL_WORKER_TOKEN", "A" * 64),
        ("INTERNAL_WORKER_TOKEN", "a" * 63),
    ],
)
def test_prod_rejects_missing_or_placeholder_worker_credentials(name, value):
    config = configuration()
    backend = config["services"]["backend"]["environment"]
    worker = config["services"]["trading-automaton"]["environment"]
    backend[name] = worker[name] = value
    if name == "INTERNAL_WORKER_ID":
        worker["AUTOMATON_WORKER_ID"] = value

    with pytest.raises(SystemExit, match="FAIL"):
        preflight.validate_contour_config(config)


def test_prod_rejects_mismatched_worker_identity_and_secret_mapping():
    config = configuration()
    config["services"]["trading-automaton"]["environment"]["AUTOMATON_WORKER_ID"] = "other-worker"

    with pytest.raises(SystemExit, match="FAIL"):
        preflight.validate_contour_config(config)


def test_prod_rejects_mismatched_core_and_worker_credentials():
    config = configuration()
    config["services"]["trading-automaton"]["environment"]["INTERNAL_WORKER_TOKEN"] = "b" * 64

    with pytest.raises(SystemExit, match="FAIL"):
        preflight.validate_contour_config(config)


@pytest.mark.parametrize(
    "mutation",
    [
        "mode",
        "contour",
        "strategy",
        "cookie",
        "origin_8443",
        "origin_cross_host",
        "origin_http",
        "project",
        "volume",
        "public",
        "network",
    ],
)
def test_prod_isolation_fails_closed(mutation):
    config = deepcopy(configuration())
    if mutation == "mode":
        config["services"]["trading-automaton"]["environment"]["BROKER_ACCESS_MODE"] = "TRADE"
    if mutation == "contour":
        config["services"]["trading-automaton"]["environment"]["APPLICATION_ENVIRONMENT"] = "TEST"
    if mutation == "strategy":
        config["services"]["trading-automaton"]["environment"]["STRATEGY_ENABLED"] = "true"
    if mutation == "cookie":
        config["services"]["backend"]["environment"]["AUTH_SESSION_COOKIE_NAME"] = "__Host-moex-session"
    if mutation == "origin_8443":
        config["services"]["backend"]["environment"]["AUTH_ALLOWED_ORIGIN"] = "https://135.136.178.252:8443"
    if mutation == "origin_cross_host":
        config["services"]["backend"]["environment"]["AUTH_ALLOWED_ORIGIN"] = "https://example.invalid"
    if mutation == "origin_http":
        config["services"]["backend"]["environment"]["AUTH_ALLOWED_ORIGIN"] = "http://135.136.178.252"
    if mutation == "project":
        config["name"] = "moex-sentinel-remote"
    if mutation == "volume":
        config["volumes"]["postgres-data"] = {"external": True, "name": "moex-sentinel-prod-postgres-data"}
    if mutation == "public":
        config["services"]["frontend"]["ports"][0]["host_ip"] = "0.0.0.0"
    if mutation == "network":
        config["networks"]["default"]["name"] = "moex-sentinel-remote_default"
    with pytest.raises(SystemExit, match="FAIL"):
        preflight.validate_contour_config(config)


@pytest.mark.parametrize("name", ["database", "migrations", "portfolio-snapshot-worker"])
def test_prod_rejects_duplicate_data_owners(name):
    config = configuration()
    config["services"][name] = {}
    with pytest.raises(SystemExit, match="FAIL"):
        preflight.validate_contour_config(config)


def test_prod_rejects_unreviewed_database_target():
    config = configuration()
    config["services"]["backend"]["environment"][
        "DATABASE_URL"
    ] = "postgresql+psycopg://sentinel:fixture@database:5432/separate_prod"
    with pytest.raises(SystemExit, match="FAIL"):
        preflight.validate_contour_config(config)


def test_remote_main_runs_release_gate_before_accessing_deployment_environment(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    for relative in (
        "pyproject.toml",
        "uv.lock",
        "src/sentinel_contracts/version.py",
        "src/moex_sentinel/__init__.py",
        "frontend/package.json",
        "frontend/package-lock.json",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    (tmp_path / "src/sentinel_contracts/version.py").write_text('SERVICE_VERSION = "9.9.9"\n')
    monkeypatch.setattr(preflight, "__file__", str(tmp_path / "deploy/remote/preflight.py"))
    with pytest.raises(SystemExit, match="Python release versions disagree"):
        preflight.main()
