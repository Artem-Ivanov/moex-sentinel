import json
from types import SimpleNamespace

import moex_sentinel.entrypoint as backend_entrypoint
import moex_sentinel.migrations.schema as schema_command
from alembic import command


def test_schema_command_upgrades_configured_database_to_head(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        schema_command,
        "Settings",
        lambda: SimpleNamespace(database_url="sqlite:///:memory:"),
    )

    def upgrade_spy(config, revision: str) -> None:
        calls.append((config.get_main_option("sqlalchemy.url"), revision))

    monkeypatch.setattr(command, "upgrade", upgrade_spy)

    assert schema_command.main() == 0
    assert calls == [("sqlite:///:memory:", "head")]
    assert json.loads(capsys.readouterr().out) == {"status": "SCHEMA_UPGRADED"}


def test_schema_command_accepts_percent_encoded_database_url(monkeypatch, capsys) -> None:
    database_url = "postgresql+psycopg://user:synthetic%40password@database/example"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        schema_command,
        "Settings",
        lambda: SimpleNamespace(database_url=database_url),
    )

    def upgrade_spy(config, revision: str) -> None:
        calls.append((config.get_main_option("sqlalchemy.url"), revision))

    monkeypatch.setattr(command, "upgrade", upgrade_spy)

    assert schema_command.main() == 0
    assert calls == [(database_url, "head")]
    assert json.loads(capsys.readouterr().out) == {"status": "SCHEMA_UPGRADED"}


def test_schema_command_returns_safe_error_without_database_url(monkeypatch, capsys) -> None:
    database_url = "sqlite:///synthetic-sensitive-database.db"
    monkeypatch.setattr(
        schema_command,
        "Settings",
        lambda: SimpleNamespace(database_url=database_url),
    )

    def fail_upgrade(_config, _revision: str) -> None:
        raise RuntimeError(database_url)

    monkeypatch.setattr(command, "upgrade", fail_upgrade)

    assert schema_command.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.err) == {"error": "SCHEMA_MIGRATION_FAILED"}
    assert database_url not in output.out
    assert database_url not in output.err


def test_backend_entrypoint_starts_server_without_schema_mutation(monkeypatch) -> None:
    migration_calls: list[str] = []
    server_calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(command, "upgrade", lambda _config, revision: migration_calls.append(revision))
    monkeypatch.setattr(
        backend_entrypoint.uvicorn,
        "run",
        lambda application, **options: server_calls.append((application, options)),
    )

    backend_entrypoint.main()

    assert migration_calls == []
    assert server_calls == [
        (
            "moex_sentinel.api.app:app",
            {
                "host": "0.0.0.0",
                "port": 8000,
                "workers": 1,
                "access_log": False,
            },
        )
    ]
