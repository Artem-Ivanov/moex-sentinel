"""Verify Analytics imports and executes without privileged application modules."""

import subprocess
import sys


def test_isolated_analytics_process_needs_no_core_worker_sdk_or_storage():
    script = """
import builtins
original = builtins.__import__
def isolated_import(name, *args, **kwargs):
    if name.split(".")[0] in {"moex_sentinel", "trading_automaton", "t_tech", "tinkoff", "sqlalchemy", "sqlite3"}:
        raise AssertionError("Forbidden Analytics dependency: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = isolated_import
from datetime import UTC, datetime
from market_analytics.app import create_app
from market_analytics.indicators import MarketIndicatorsService
from sentinel_contracts.analytics import AdaptiveThresholds
app = create_app(object(), now=lambda: datetime.now(UTC))
result = MarketIndicatorsService().calculate((), AdaptiveThresholds("0.5", "0.3", "STRATEGY"))
assert result.mean_20 is None
assert result.source == "STRATEGY"
assert any(route.path == "/internal/v1/analytics/snapshots" for route in app.routes)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
