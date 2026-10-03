import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect

import moex_sentinel.entrypoint as backend_entrypoint
import moex_sentinel.migrations.schema as schema_command
from alembic import command
from alembic.config import Config
from moex_sentinel.config import Settings as CoreSettings
from moex_sentinel.storage.models import Base
from moex_sentinel.storage.schema_revision import current_schema_revision


def test_schema_command_upgrades_configured_database_to_head(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        schema_command,
        "MigrationSettings",
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
        "MigrationSettings",
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
        "MigrationSettings",
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


@pytest.mark.parametrize("irrelevant_configuration", [False, True])
def test_schema_command_migrates_with_database_configuration_only(
    monkeypatch, tmp_path, capsys, irrelevant_configuration
):
    alembic_ini = Path("alembic.ini").resolve()
    workdir = tmp_path / "working" / "schema"
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(schema_command, "Config", lambda _: Config(str(alembic_ini)))
    for name in CoreSettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    database_url = f"sqlite:///{tmp_path / 'schema-only.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    if irrelevant_configuration:
        monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
        monkeypatch.setenv("BROKER_ACCESS_MODE", "invalid")
        monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "invalid")
        monkeypatch.setenv("AUTH_PASSWORD_HASH", "invalid")
    assert schema_command.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "SCHEMA_UPGRADED"}
    engine = create_engine(database_url)
    try:
        assert current_schema_revision(engine) == "0003_user_broker_archive"
        assert set(inspect(engine).get_table_names()) == {*Base.metadata.tables, "alembic_version"}
    finally:
        engine.dispose()


def test_schema_command_rejects_unsupported_database_dialect(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "mysql://synthetic:private@localhost/schema")
    monkeypatch.setattr(command, "upgrade", lambda *args: pytest.fail("unsupported dialect reached Alembic"))
    assert schema_command.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.err) == {"error": "SCHEMA_MIGRATION_FAILED"}
    assert "private" not in output.out + output.err
