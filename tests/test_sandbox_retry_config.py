import pytest
from pydantic import ValidationError

from moex_sentinel.config import Settings
from trading_automaton.config import AutomatonSettings


@pytest.mark.parametrize("settings_class", [Settings, AutomatonSettings])
def test_sandbox_retry_limit_has_default_and_reads_environment(monkeypatch, settings_class):
    monkeypatch.delenv("SANDBOX_RETRY_LIMIT", raising=False)
    assert settings_class(_env_file=None).sandbox_retry_limit == 5
    monkeypatch.setenv("SANDBOX_RETRY_LIMIT", "2")
    assert settings_class(_env_file=None).sandbox_retry_limit == 2
    monkeypatch.setenv("SANDBOX_RETRY_LIMIT", "0")
    assert settings_class(_env_file=None).sandbox_retry_limit == 0


@pytest.mark.parametrize("settings_class", [Settings, AutomatonSettings])
def test_sandbox_retry_limit_rejects_negative_values(monkeypatch, settings_class):
    monkeypatch.setenv("SANDBOX_RETRY_LIMIT", "-1")
    with pytest.raises(ValidationError):
        settings_class(_env_file=None)
