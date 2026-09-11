"""HTTP contract for the persisted trading summary."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.domain.portfolio import BrokerReadError
from moex_sentinel.domain.trading_summary import (
    CurrencyTradingSummary,
    TradingPnlPeriod,
    TradingSummaryView,
)

NOW = datetime(2026, 8, 15, 10, tzinfo=UTC)


class Execute:
    def __init__(self, result: TradingSummaryView) -> None:
        self.result = result

    def execute(self) -> TradingSummaryView:
        return self.result


def period(value: str | None, days: int, *, complete: bool = True) -> TradingPnlPeriod:
    return TradingPnlPeriod(
        None if value is None else Decimal(value),
        NOW - timedelta(days=days),
        NOW,
        complete,
    )


def test_trading_summary_contract_reports_currencies_periods_and_safe_errors(monkeypatch, tmp_path) -> None:
    summary = TradingSummaryView(
        NOW,
        (
            CurrencyTradingSummary(
                "RUB",
                Decimal("150.000000000"),
                Decimal("25.000000000"),
                period("7.000000000", 1),
                period("-2.000000000", 2, complete=False),
                period(None, 30, complete=False),
            ),
            CurrencyTradingSummary(
                "USD",
                Decimal("20.000000000"),
                Decimal("3.000000000"),
                period("0.000000000", 1),
                period("1.000000000", 7),
                period("3.000000000", 30),
            ),
        ),
        (BrokerReadError("broker-1", "Synthetic broker", "account-1", "BROKER_UNAVAILABLE", "Недоступно"),),
    )
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory: SimpleNamespace(view_trading_summary=Execute(summary)),
    )

    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'summary.db'}")) as client:
        response = client.get("/api/trading/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["captured_at"] == "2026-08-15T10:00:00Z"
    assert body["currencies"][0]["pnl_24h"] == {
        "value": "7.000000000",
        "from": "2026-08-14T10:00:00Z",
        "to": "2026-08-15T10:00:00Z",
        "complete": True,
    }
    assert body["currencies"][0]["pnl_7d"]["value"] == "-2.000000000"
    assert body["currencies"][0]["pnl_7d"]["complete"] is False
    assert body["currencies"][0]["pnl_30d"]["value"] is None
    assert [item["currency"] for item in body["currencies"]] == ["RUB", "USD"]
    assert body["errors"] == [
        {
            "broker_id": "broker-1",
            "broker_name": "Synthetic broker",
            "account_id": "account-1",
            "code": "BROKER_UNAVAILABLE",
            "message": "Недоступно",
        }
    ]


def test_trading_summary_contract_has_explicit_empty_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "moex_sentinel.api.app.build_application_usecases",
        lambda _factory: SimpleNamespace(view_trading_summary=Execute(TradingSummaryView(None, (), ()))),
    )

    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'empty-summary.db'}")) as client:
        response = client.get("/api/trading/summary")

    assert response.status_code == 200
    assert response.json() == {"captured_at": None, "currencies": [], "errors": []}
