"""Static acceptance of the isolated portfolio snapshot runtime."""

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parents[2]


def test_snapshot_worker_has_no_sqlite_or_backend_dependency() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    worker = services["portfolio-snapshot-worker"]

    assert "AUTOMATON_DATABASE_URL" not in worker["environment"]
    assert "backend" not in worker.get("depends_on", {})
    assert "volumes" not in worker
    assert worker["environment"]["PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS"] == (
        "${PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS:-60}"
    )
    assert "redis" not in services
    assert "celery" not in services
