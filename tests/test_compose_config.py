from pathlib import Path
from typing import Any

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
        "STRATEGY_ENABLED": "${STRATEGY_ENABLED:-true}",
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
    assert snapshot_worker["build"]["dockerfile"] == "docker/portfolio-snapshot-worker.Dockerfile"
    assert snapshot_worker["depends_on"] == {"database": {"condition": "service_healthy"}}
    assert snapshot_worker["environment"] == {
        "DATABASE_URL": (
            "postgresql+psycopg://${POSTGRES_USER}:"
            "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be configured}@database:5432/${POSTGRES_DB}"
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
