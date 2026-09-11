import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parents[1]
ACTIVE_DOCUMENTS = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "AGENT_BRIEF.md",
    PROJECT_ROOT / "docs/development.md",
    PROJECT_ROOT / "docs/trading-strategy.md",
    PROJECT_ROOT / "docs/clean-slate-cutover.md",
)
STRATEGY_ENV = (
    "STRATEGY_BUY_ORDER_LOTS",
    "STRATEGY_STOP_LOSS_PERCENT",
    "STRATEGY_TAKE_PROFIT_PERCENT",
    "STRATEGY_AVERAGING_STEP_PERCENT",
    "STRATEGY_PARTIAL_TAKE_PROFIT_PERCENT",
    "STRATEGY_PARTIAL_SELL_PERCENT",
    "STRATEGY_MAX_PARTIAL_SELL_STEPS",
    "STRATEGY_ORDER_TTL_SECONDS",
    "STRATEGY_ORDER_RETRY_LIMIT",
    "STRATEGY_CORE_RETRY_LIMIT",
    "STRATEGY_ENABLED",
)
MARKDOWN_LINK = re.compile(r"\[[^]]+]\((?![a-z]+:|#)([^)]+)\)")


def test_active_docs_describe_strategy_and_typed_runtime() -> None:
    strategy = (PROJECT_ROOT / "docs/trading-strategy.md").read_text(encoding="utf-8")
    for name in STRATEGY_ENV:
        assert name in strategy

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    for endpoint in (
        "/internal/automation-commands",
        "/internal/automation-statuses",
        "/internal/automation-facts",
    ):
        assert endpoint in readme
    assert "HOLD/BOOTSTRAPPING" in readme
    assert "IN_WORK" in readme
    assert "mypy" not in "\n".join(path.read_text(encoding="utf-8").lower() for path in ACTIVE_DOCUMENTS)


def test_cutover_runbook_keeps_destructive_cleanup_behind_confirmation() -> None:
    runbook = (PROJECT_ROOT / "docs/clean-slate-cutover.md").read_text(encoding="utf-8")
    for fragment in (
        "STRATEGY_ENABLED=false",
        "heartbeat",
        "UNCERTAIN",
        "FILLED",
        "POSTGRES_VOLUME_NAME",
        "AUTOMATON_VOLUME_NAME",
        "STOP — требуется отдельное подтверждение",
    ):
        assert fragment in runbook


def test_relative_markdown_links_resolve() -> None:
    for document in ACTIVE_DOCUMENTS:
        content = document.read_text(encoding="utf-8")
        for target in MARKDOWN_LINK.findall(content):
            path = target.split("#", maxsplit=1)[0]
            if path:
                assert (document.parent / path).resolve().exists(), f"Broken link {target!r} in {document}"


def test_openapi_documents_trading_summary() -> None:
    contract = yaml.safe_load((PROJECT_ROOT / "docs/contracts/openapi.yaml").read_text(encoding="utf-8"))

    response = contract["paths"]["/api/trading/summary"]["get"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/TradingSummarySchema"}
    period = contract["components"]["schemas"]["TradingPnlPeriodSchema"]
    assert period["properties"]["from"]["anyOf"][1] == {"type": "null"}
    assert period["properties"]["value"]["anyOf"][0]["type"] == "string"
    assert set(period["required"]) == {"value", "from", "to", "complete"}


def test_runtime_docs_describe_snapshot_worker_and_incomplete_history() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    development = (PROJECT_ROOT / "docs/development.md").read_text(encoding="utf-8")
    runbook = (PROJECT_ROOT / "docs/clean-slate-cutover.md").read_text(encoding="utf-8")

    for content in (readme, development, runbook):
        assert "portfolio-snapshot-worker" in content
        assert "/api/trading/summary" in content
    assert "PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS" in development
    assert "60" in development
    assert "неполн" in readme.lower()
