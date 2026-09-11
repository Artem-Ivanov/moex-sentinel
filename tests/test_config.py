import pytest
from pydantic import ValidationError

from moex_sentinel.config import Settings


def test_snapshot_worker_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.portfolio_snapshot_interval_seconds == 60
    assert settings.portfolio_snapshot_retry_limit == 3
    assert settings.portfolio_snapshot_retry_base_seconds == 1.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("portfolio_snapshot_interval_seconds", 59),
        ("portfolio_snapshot_retry_limit", -1),
        ("portfolio_snapshot_retry_base_seconds", 0),
    ],
)
def test_snapshot_worker_settings_reject_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
