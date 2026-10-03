import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parents[1]


def load_compose() -> dict[str, Any]:
    with (PROJECT_ROOT / "compose.yml").open(encoding="utf-8") as compose_file:
        loaded = yaml.safe_load(compose_file)
    assert isinstance(loaded, dict)
    return loaded


def test_compose_has_expected_services_and_single_backend_process() -> None:
    compose = load_compose()
    backend = compose["services"]["backend"]
    automaton = compose["services"]["trading-automaton"]
    snapshot_worker = compose["services"]["portfolio-snapshot-worker"]
    frontend = compose["services"]["frontend"]
    database = compose["services"]["database"]
    migrations = compose["services"]["migrations"]

    base = compose["services"]["python-base"]
    assert set(compose["services"]) == {
        "python-base",
        "database",
        "migrations",
        "backend",
        "analytics",
        "portfolio-snapshot-worker",
        "trading-automaton",
        "frontend",
    }
    assert base["profiles"] == ["build"]
    assert base["build"]["dockerfile"] == "docker/python-base.Dockerfile"
    assert database["image"] == "postgres:16-alpine"
    assert database["healthcheck"]["test"] == ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
    assert database["volumes"] == ["postgres-data:/var/lib/postgresql/data"]
    assert compose["volumes"]["postgres-data"]["external"] is True
    assert migrations["profiles"] == ["migrations"]
    assert migrations["build"]["dockerfile"] == "docker/migrations.Dockerfile"
    assert migrations["depends_on"]["database"]["condition"] == "service_healthy"
    assert migrations["command"] == ["moex-migrate-schema"]
    assert migrations["environment"]["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert frontend["depends_on"]["backend"]["condition"] == "service_healthy"
    assert automaton["depends_on"]["backend"]["condition"] == "service_healthy"
    assert automaton["volumes"] == ["automaton-data:/app/data"]
    assert automaton["environment"] == {
        "APPLICATION_ENVIRONMENT": "${APPLICATION_ENVIRONMENT:?APPLICATION_ENVIRONMENT must be configured}",
        "BROKER_ACCESS_MODE": "${BROKER_ACCESS_MODE:?BROKER_ACCESS_MODE must be configured}",
        "AUTOMATON_DATABASE_URL": "sqlite:////app/data/trading_automaton.db",
        "AUTOMATON_HEARTBEAT_INTERVAL_SECONDS": "3",
        "CORE_URL": "http://backend:8000",
        "ANALYTICS_URL": "http://analytics:8001",
        "FACT_OUTBOX_BATCH_SIZE": "100",
        "FACT_OUTBOX_DEADLINE_MS": "1000",
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "SANDBOX_RETRY_LIMIT": "${SANDBOX_RETRY_LIMIT:-5}",
        "STRATEGY_BUY_ORDER_LOTS": "${STRATEGY_BUY_ORDER_LOTS:-1}",
        "STRATEGY_STOP_LOSS_PERCENT": "${STRATEGY_STOP_LOSS_PERCENT:-5}",
        "STRATEGY_TAKE_PROFIT_PERCENT": "${STRATEGY_TAKE_PROFIT_PERCENT:-6}",
        "STRATEGY_AVERAGING_STEP_PERCENT": "${STRATEGY_AVERAGING_STEP_PERCENT:-0.5}",
        "STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT": "${STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT:-0.5}",
        "STRATEGY_PARTIAL_SELL_PERCENT": "${STRATEGY_PARTIAL_SELL_PERCENT:-25}",
        "STRATEGY_MAX_PARTIAL_SELL_STEPS": "${STRATEGY_MAX_PARTIAL_SELL_STEPS:-3}",
        "STRATEGY_ORDER_TTL_SECONDS": "${STRATEGY_ORDER_TTL_SECONDS:-10}",
        "STRATEGY_ORDER_RETRY_LIMIT": "${STRATEGY_ORDER_RETRY_LIMIT:-3}",
        "STRATEGY_CORE_RETRY_LIMIT": "${STRATEGY_CORE_RETRY_LIMIT:-5}",
        "STRATEGY_ENABLED": "${STRATEGY_ENABLED:-false}",
    }
    assert "volumes" not in backend
    assert backend["environment"]["SANDBOX_RETRY_LIMIT"] == "${SANDBOX_RETRY_LIMIT:-5}"
    analytics = compose["services"]["analytics"]
    assert analytics["environment"] == {"ANALYTICS_CORE_URL": "http://backend:8000"}
    assert "volumes" not in analytics
    assert "ports" not in analytics
    assert automaton["depends_on"]["analytics"]["condition"] == "service_healthy"
    assert backend["depends_on"]["database"]["condition"] == "service_healthy"
    assert backend["environment"]["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert "ports" not in backend
    assert backend["environment"]["AUTH_USERNAME"] == "${AUTH_USERNAME:?AUTH_USERNAME must be configured}"
    assert backend["environment"]["AUTH_PASSWORD_HASH"] == (
        "${AUTH_PASSWORD_HASH:?AUTH_PASSWORD_HASH must be configured}"
    )
    assert backend["environment"]["AUTH_INSECURE_LOOPBACK"] == "${AUTH_INSECURE_LOOPBACK:-false}"
    assert snapshot_worker["build"]["dockerfile"] == "docker/portfolio-snapshot-worker.Dockerfile"
    assert snapshot_worker["depends_on"] == {"database": {"condition": "service_healthy"}}
    assert snapshot_worker["environment"] == {
        "APPLICATION_ENVIRONMENT": "${APPLICATION_ENVIRONMENT:?APPLICATION_ENVIRONMENT must be configured}",
        "BROKER_ACCESS_MODE": "${BROKER_ACCESS_MODE:?BROKER_ACCESS_MODE must be configured}",
        "DATABASE_URL": (
            "postgresql+psycopg://${POSTGRES_USER}:"
            "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be configured}@${DATABASE_HOST:-database}:5432/${POSTGRES_DB}"
        ),
        "LOG_FORMAT": "json",
        "LOG_LEVEL": "INFO",
        "PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS": "${PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS:-60}",
    }
    assert "ports" not in snapshot_worker
    assert "volumes" not in snapshot_worker
    assert frontend["ports"] == ["127.0.0.1:${APP_PORT:-8080}:8080"]
    assert frontend["read_only"] is True
    assert "/tmp" in frontend["tmpfs"]  # noqa: S108 - container tmpfs mount contract

    backend_dockerfile = (PROJECT_ROOT / "docker/backend.Dockerfile").read_text(encoding="utf-8")
    base_dockerfile = (PROJECT_ROOT / "docker/python-base.Dockerfile").read_text(encoding="utf-8")
    backend_entrypoint = (PROJECT_ROOT / "src/moex_sentinel/entrypoint.py").read_text(encoding="utf-8")
    frontend_dockerfile = (PROJECT_ROOT / "docker/frontend.Dockerfile").read_text(encoding="utf-8")
    assert "USER sentinel" in backend_dockerfile
    assert "FROM moex-sentinel-python-base:local" in backend_dockerfile
    assert "FROM python:3.12-slim" in base_dockerfile
    assert "pyproject.toml" in base_dockerfile
    assert 'CMD ["python", "-m", "moex_sentinel.entrypoint"]' in backend_dockerfile
    automaton_dockerfile = (PROJECT_ROOT / "docker/automaton.Dockerfile").read_text(encoding="utf-8")
    snapshot_worker_dockerfile = (PROJECT_ROOT / "docker/portfolio-snapshot-worker.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "USER sentinel" in automaton_dockerfile
    assert "FROM moex-sentinel-python-base:local" in automaton_dockerfile
    assert 'CMD ["python", "-m", "trading_automaton"]' in automaton_dockerfile
    assert "USER sentinel" in snapshot_worker_dockerfile
    assert 'CMD ["portfolio-snapshot-worker"]' in snapshot_worker_dockerfile
    assert "workers=1" in backend_entrypoint.replace(" ", "")
    assert "nginxinc/nginx-unprivileged" in frontend_dockerfile


def test_compose_does_not_receive_secret_environment_values() -> None:
    compose = load_compose()
    forbidden_names = ("TOKEN", "PASSWORD", "SECRET", "API_KEY")

    for service in compose["services"].values():
        environment = service.get("environment", {})
        for name, value in environment.items():
            if any(word in name.upper() for word in forbidden_names):
                assert isinstance(value, str)
                assert value.startswith("${")


def test_worker_recovery_volume_has_stable_external_identity() -> None:
    compose = load_compose()

    assert compose["services"]["trading-automaton"]["volumes"] == ["automaton-data:/app/data"]
    assert compose["volumes"]["automaton-data"] == {
        "external": True,
        "name": "${AUTOMATON_VOLUME_NAME:-moex-sentinel_automaton-data}",
    }


def test_gitignore_excludes_ide_metadata() -> None:
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".idea/" in gitignore


def test_remote_auth_configuration_fails_closed() -> None:
    remote = (PROJECT_ROOT / "deploy/remote/compose.remote.yml").read_text(encoding="utf-8")
    assert 'AUTH_INSECURE_LOOPBACK: "false"' in remote


def test_vite_dev_server_listens_only_on_loopback() -> None:
    package = json.loads((PROJECT_ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["dev"] == "vite --host 127.0.0.1"


def test_remote_preflight_rejects_invalid_auth_without_echoing_secret() -> None:
    module_spec = importlib.util.spec_from_file_location(
        "remote_preflight", PROJECT_ROOT / "deploy/remote/preflight.py"
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    preflight = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(preflight)

    valid = "scrypt:16384:8:5:" + "a" * 32 + ":" + "b" * 64
    preflight.validate_auth_config({"AUTH_INSECURE_LOOPBACK": "false", "AUTH_PASSWORD_HASH": valid})

    for value in ("", "replace-with-scrypt-verifier", "scrypt:16384:8:5:bad:bad"):
        with pytest.raises(SystemExit) as error:
            preflight.validate_auth_config({"AUTH_INSECURE_LOOPBACK": "false", "AUTH_PASSWORD_HASH": value})
        assert str(error.value) == "FAIL: Set AUTH_PASSWORD_HASH to a generated scrypt verifier."

    with pytest.raises(SystemExit) as error:
        preflight.validate_auth_config({"AUTH_INSECURE_LOOPBACK": "true", "AUTH_PASSWORD_HASH": valid})
    assert str(error.value) == "FAIL: Remote deployment requires AUTH_INSECURE_LOOPBACK=false."


def test_contour_and_access_mode_propagate_to_all_broker_owners() -> None:
    services = load_compose()["services"]
    for name in ("backend", "portfolio-snapshot-worker", "trading-automaton"):
        environment = services[name]["environment"]
        assert (
            environment["APPLICATION_ENVIRONMENT"]
            == "${APPLICATION_ENVIRONMENT:?APPLICATION_ENVIRONMENT must be configured}"
        )
        assert environment["BROKER_ACCESS_MODE"] == "${BROKER_ACCESS_MODE:?BROKER_ACCESS_MODE must be configured}"
    assert (
        services["backend"]["environment"]["AUTH_ALLOWED_ORIGIN"]
        == "${AUTH_ALLOWED_ORIGIN:?AUTH_ALLOWED_ORIGIN must be configured}"
    )
    assert services["trading-automaton"]["environment"]["STRATEGY_ENABLED"] == "${STRATEGY_ENABLED:-false}"


def test_remote_shared_database_compose_resolves_without_daemon(monkeypatch):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI unavailable")
    configs = {}
    for contour, wrapper, example in (
        ("TEST", "compose.sh", ".env.example"),
        ("PROD", "compose.production.sh", ".env.production.example"),
    ):
        environment = os.environ.copy()
        environment.update(
            line.split("=", 1)
            for line in (PROJECT_ROOT / "deploy/remote" / example).read_text().splitlines()
            if line and not line.startswith("#")
        )
        environment["REMOTE_ENV_FILE"] = "/dev/null"
        result = subprocess.run(
            [
                shutil.which("sh"),
                str(PROJECT_ROOT / "deploy/remote" / wrapper),
                "--profile",
                "trading",
                "--profile",
                "migrations",
                "config",
                "--format",
                "json",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        configs[contour] = json.loads(result.stdout)
    test, prod = configs["TEST"], configs["PROD"]
    assert {"database", "migrations", "portfolio-snapshot-worker"} <= test["services"].keys()
    assert not {"database", "migrations", "portfolio-snapshot-worker"} & prod["services"].keys()
    assert "postgres-data" not in prod["volumes"]
    assert (
        test["services"]["backend"]["environment"]["DATABASE_URL"]
        == prod["services"]["backend"]["environment"]["DATABASE_URL"]
    )
    assert test["services"]["portfolio-snapshot-worker"]["environment"]["PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS"] == "true"
    assert test["services"]["database"]["networks"]["data"]["aliases"] == ["sentinel-shared-postgres"]
    assert set(prod["services"]["backend"]["networks"]) == {"default", "data"}
    assert test["networks"]["default"]["name"] != prod["networks"]["default"]["name"]
    assert test["volumes"]["automaton-data"]["name"] != prod["volumes"]["automaton-data"]["name"]

    module_spec = importlib.util.spec_from_file_location(
        "shared_preflight", PROJECT_ROOT / "deploy/remote/preflight.py"
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    preflight = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(preflight)
    preflight.validate_shared_database_pair(test, prod)
    from copy import deepcopy  # noqa: PLC0415

    for mutation in (
        "database",
        "network",
        "collector",
        "alias",
        "duplicate",
        "client_network",
        "project",
        "app_network",
        "pg_volume",
        "worker_volume",
    ):
        bad = deepcopy(test)
        if mutation == "database":
            bad["services"]["migrations"]["environment"]["DATABASE_URL"] += "_wrong"
        elif mutation == "network":
            bad["networks"]["data"]["name"] = "other-network"
        elif mutation == "collector":
            bad["services"]["portfolio-snapshot-worker"]["environment"]["PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS"] = "false"
        elif mutation == "alias":
            bad["services"]["database"]["networks"]["data"]["aliases"] = ["database"]
        elif mutation == "duplicate":
            bad["services"]["second-database"] = {"image": "postgres:16-alpine"}
        elif mutation == "client_network":
            bad["services"]["migrations"]["networks"].pop("data")
        elif mutation == "project":
            bad["name"] = "moex-sentinel-prod"
        elif mutation == "app_network":
            bad["networks"]["default"]["name"] = prod["networks"]["default"]["name"]
        elif mutation == "pg_volume":
            bad["volumes"]["postgres-data"]["name"] = "moex-sentinel-prod-postgres-data"
        elif mutation == "worker_volume":
            bad["volumes"]["automaton-data"]["name"] = prod["volumes"]["automaton-data"]["name"]
        with pytest.raises(SystemExit, match="FAIL"):
            preflight.validate_shared_database_pair(bad, prod)


def test_production_wrapper_defaults_to_separate_environment_file():
    wrapper = (PROJECT_ROOT / "deploy/remote/compose.production.sh").read_text()
    example = (PROJECT_ROOT / "deploy/remote/.env.production.example").read_text()
    assert ".env.production" in wrapper
    assert "Copy to deploy/remote/.env.production," in example


def test_core_analytics_target_is_explicit_test_and_empty_in_prod_override():
    compose = load_compose()
    assert compose["services"]["backend"]["environment"]["ANALYTICS_URL"] == "http://analytics:8001"

    class ProductionLoader(yaml.SafeLoader):
        pass

    ProductionLoader.add_constructor("!reset", lambda loader, node: None)
    ProductionLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))
    remote = yaml.load(
        (PROJECT_ROOT / "deploy/remote/compose.remote.yml").read_text(),
        Loader=ProductionLoader,  # noqa: S506 - SafeLoader subclass only handles Compose tags.
    )
    assert remote["services"]["backend"]["environment"]["ANALYTICS_URL"] == "http://analytics:8001"
    production = yaml.load(
        (PROJECT_ROOT / "deploy/remote/compose.production.yml").read_text(),
        Loader=ProductionLoader,  # noqa: S506 - SafeLoader subclass only handles Compose tags.
    )
    assert production["services"]["backend"]["environment"]["ANALYTICS_URL"] == ""
