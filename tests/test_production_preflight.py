"""Offline safety gates do not contact a daemon or broker."""

import importlib.util
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
                    "AUTH_SESSION_COOKIE_NAME": "__Host-moex-prod-session",
                    "AUTH_ALLOWED_ORIGIN": "https://135.136.178.252:8443",
                    "DATABASE_URL": "postgresql+psycopg://sentinel:fixture@sentinel-shared-postgres:5432/moex_sentinel",
                }
            },
            "trading-automaton": {"environment": {**environment, "STRATEGY_ENABLED": "false"}},
            "frontend": {"ports": [{"host_ip": "127.0.0.1", "published": "8081", "target": 8080}]},
        },
        "volumes": {
            "automaton-data": {"external": True, "name": "moex-sentinel-prod-automaton-data"},
        },
    }


def test_prod_readonly_configuration_is_accepted():
    preflight.validate_contour_config(configuration())


@pytest.mark.parametrize(
    "mutation", ["mode", "contour", "strategy", "cookie", "origin", "project", "volume", "public", "network"]
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
    if mutation == "origin":
        config["services"]["backend"]["environment"]["AUTH_ALLOWED_ORIGIN"] = "https://135.136.178.252"
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
