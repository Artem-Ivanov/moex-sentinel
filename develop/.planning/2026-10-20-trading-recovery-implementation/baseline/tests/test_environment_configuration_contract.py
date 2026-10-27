import re
from pathlib import Path

from market_analytics.config import AnalyticsSettings
from moex_sentinel.config import Settings as CoreSettings
from trading_automaton.config import AutomatonSettings, StrategySettings

PROJECT_ROOT = Path(__file__).parents[1]
TEMPLATES = (
    PROJECT_ROOT / ".env.example",
    PROJECT_ROOT / "deploy/remote/.env.example",
    PROJECT_ROOT / "deploy/remote/.env.production.example",
)
CATALOG = PROJECT_ROOT / "docs/configuration/environment.md"
COMPOSE_FILES = (
    PROJECT_ROOT / "compose.yml",
    PROJECT_ROOT / "deploy/remote/compose.remote.yml",
    PROJECT_ROOT / "deploy/remote/compose.production.yml",
)


def setting_env_names(model: type) -> set[str]:
    names = set()
    for field_name, field in model.model_fields.items():
        alias = field.validation_alias
        names.add(alias if isinstance(alias, str) else field_name.upper())
    return names


def template_names(path: Path) -> set[str]:
    return {
        line.split("=", maxsplit=1)[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.lstrip().startswith("#") and "=" in line
    }


def catalog_names(source: str) -> set[str]:
    return set(re.findall(r"\| `([A-Z][A-Z0-9_]*)`[^|]*\|", source))


def runtime_env_names() -> set[str]:
    names = set()
    for model in (CoreSettings, AutomatonSettings, StrategySettings, AnalyticsSettings):
        names.update(setting_env_names(model))
    return names


def test_every_setting_is_documented_shipped_and_composed() -> None:
    env_names = runtime_env_names()
    catalog = CATALOG.read_text(encoding="utf-8")
    documented = catalog_names(catalog)
    compose_source = "\n".join(path.read_text(encoding="utf-8") for path in COMPOSE_FILES)

    # Core's container setting keeps its existing name; the host-facing alias prevents
    # it from being confused with Worker ANALYTICS_URL.
    template_names_expected = env_names - {"DATABASE_URL", "ANALYTICS_URL"}
    template_names_expected.add("CORE_ANALYTICS_URL")
    compose_names_expected = env_names - {"DATABASE_URL", "ANALYTICS_URL"}
    compose_names_expected.add("CORE_ANALYTICS_URL")

    assert env_names <= documented
    assert template_names_expected <= documented
    assert compose_names_expected <= set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    for path in TEMPLATES:
        names = template_names(path)
        assert template_names_expected <= names, f"{path.relative_to(PROJECT_ROOT)} omits settings"


def test_every_compose_host_variable_has_a_catalog_entry() -> None:
    compose_source = "\n".join(path.read_text(encoding="utf-8") for path in COMPOSE_FILES)
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    documented = catalog_names(CATALOG.read_text(encoding="utf-8"))
    assert used <= documented


def test_examples_preserve_safe_defaults_and_contour_aliases() -> None:
    local = template_names(TEMPLATES[0])
    remote_test = template_names(TEMPLATES[1])
    remote_prod = template_names(TEMPLATES[2])
    local_values = dict(
        line.split("=", maxsplit=1)
        for line in TEMPLATES[0].read_text(encoding="utf-8").splitlines()
        if line and not line.lstrip().startswith("#") and "=" in line
    )
    prod_values = dict(
        line.split("=", maxsplit=1)
        for line in TEMPLATES[2].read_text(encoding="utf-8").splitlines()
        if line and not line.lstrip().startswith("#") and "=" in line
    )

    assert {"BROKER_ACCESS_MODE", "STRATEGY_ENABLED"} <= local
    assert local_values["BROKER_ACCESS_MODE"] == "READ_ONLY"
    assert local_values["STRATEGY_ENABLED"] == "false"
    assert local_values["DAILY_ADMISSION_PREPARATION_ENABLED"] == "false"
    assert local_values["CORE_ANALYTICS_URL"] == "http://analytics:8001"
    assert remote_test <= catalog_names(CATALOG.read_text(encoding="utf-8"))
    assert remote_prod <= catalog_names(CATALOG.read_text(encoding="utf-8"))
    assert prod_values["DAILY_ADMISSION_PREPARATION_ENABLED"] == "true"
    assert prod_values["BROKER_ACCESS_MODE"] == "READ_ONLY"
    assert prod_values["STRATEGY_ENABLED"] == "false"
    assert prod_values["CORE_ANALYTICS_URL"] == ""
    assert prod_values["AUTOMATON_WORKER_ID"] == ""


def test_remote_collector_scope_stays_forced() -> None:
    remote = (PROJECT_ROOT / "deploy/remote/compose.remote.yml").read_text(encoding="utf-8")
    assert 'PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS: "true"' in remote
    assert 'ANALYTICS_URL: "${CORE_ANALYTICS_URL-http://analytics:8001}"' in remote
    production = (PROJECT_ROOT / "deploy/remote/compose.production.yml").read_text(encoding="utf-8")
    assert 'ANALYTICS_URL: "${CORE_ANALYTICS_URL:-}"' in production
