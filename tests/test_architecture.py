import ast
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
FORBIDDEN = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\blegacy\b",
        r"\bV2\b",
        r"_v2\b",
        r"FACT_INGRESS_VERSION",
        r"/internal/v2",
        r"strategy_templates",
        r"automation_strategies",
        r"instrument_strategy_defaults",
    )
)
ACTIVE_DOCUMENTS = (
    "README.md",
    "AGENT_BRIEF.md",
    "docs/development.md",
    "docs/trading-strategy.md",
    "docs/clean-slate-cutover.md",
)
APP_PACKAGES = {"moex_sentinel", "trading_automaton", "market_analytics"}
PURE_EXTERNALS = {"pydantic", "typing_extensions"}
TRANSPORT_EXTERNALS = {"httpx", "grpc", "t_tech"}
BUSINESS_LAYERS = {"domain", "services", "usecases", "runtime", "analytics_pure"}
FIXTURE_MODULES = {
    "moex_sentinel",
    "moex_sentinel.domain",
    "moex_sentinel.services",
    "moex_sentinel.storage",
    "moex_sentinel.storage.repositories",
    "trading_automaton",
    "trading_automaton.adapters",
    "trading_automaton.domain",
    "trading_automaton.usecases",
    "market_analytics",
    "market_analytics.service",
    "sentinel_contracts",
    "sentinel_contracts.base",
}
LAYER_RULES = {
    ("moex_sentinel", "domain"): {"domain", "config", "root"},
    ("moex_sentinel", "services"): {"domain", "services", "config", "root"},
    ("moex_sentinel", "usecases"): {"domain", "services", "usecases", "config", "root"},
    ("moex_sentinel", "storage"): {"domain", "storage", "root"},
    ("moex_sentinel", "adapters"): {"domain", "services", "adapters", "root"},
    ("moex_sentinel", "views"): {"domain", "services", "usecases", "views", "api", "root"},
    ("moex_sentinel", "api"): {
        "domain",
        "services",
        "usecases",
        "views",
        "api",
        "adapters",
        "storage",
        "composition",
        "config",
        "root",
    },
    ("moex_sentinel", "composition"): {
        "domain",
        "services",
        "usecases",
        "storage",
        "adapters",
        "views",
        "api",
        "config",
        "root",
    },
    ("moex_sentinel", "entrypoint"): {"composition", "api", "config", "root"},
    ("moex_sentinel", "config"): {"config", "domain", "root"},
    ("moex_sentinel", "migrations"): {"migrations", "storage", "domain", "config", "root"},
    ("moex_sentinel", "worker_entrypoint"): {"composition", "storage", "domain", "services", "config", "root"},
    ("trading_automaton", "domain"): {"domain", "config", "root"},
    ("trading_automaton", "services"): {"domain", "services", "config", "root"},
    ("trading_automaton", "usecases"): {"domain", "services", "usecases", "config", "root"},
    ("trading_automaton", "runtime"): {"domain", "services", "usecases", "runtime", "config", "root"},
    ("trading_automaton", "storage"): {"domain", "storage", "root"},
    ("trading_automaton", "adapters"): {"domain", "services", "adapters", "root"},
    ("trading_automaton", "composition"): {
        "domain",
        "services",
        "usecases",
        "runtime",
        "storage",
        "adapters",
        "config",
        "root",
    },
    ("trading_automaton", "entrypoint"): {"composition", "config", "root"},
    ("trading_automaton", "config"): {"config", "domain", "root"},
    ("market_analytics", "analytics_pure"): {"analytics_pure", "root"},
    ("market_analytics", "analytics_transport"): {"analytics_pure", "analytics_transport", "root"},
    ("market_analytics", "entrypoint"): {"analytics_pure", "analytics_transport", "entrypoint", "root"},
}


def test_runtime_tree_contains_no_compatibility_identifiers() -> None:
    files = [
        *(PROJECT_ROOT / "src").rglob("*.py"),
        *(PROJECT_ROOT / "frontend/src").rglob("*.ts"),
        *(PROJECT_ROOT / "frontend/src").rglob("*.vue"),
        PROJECT_ROOT / "compose.yml",
        PROJECT_ROOT / ".env.example",
        PROJECT_ROOT / "pyproject.toml",
        *(PROJECT_ROOT / path for path in ACTIVE_DOCUMENTS),
    ]
    violations: list[str] = []
    for path in files:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            if pattern.search(content):
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {pattern.pattern}")
    assert violations == [], "\n".join(violations)


def _layer(module: str) -> tuple[str | None, str]:
    parts = module.split(".")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    owner = parts[0]
    if owner == "sentinel_contracts":
        return owner, "shared"
    if owner not in APP_PACKAGES:
        return None, "external"
    if len(parts) == 1:
        return owner, "root"
    if owner == "market_analytics":
        if parts[1] == "__main__":
            return owner, "entrypoint"
        return owner, "analytics_transport" if parts[1] in {"app", "market_source"} else "analytics_pure"
    layer = parts[1]
    if layer == "__main__":
        return owner, "entrypoint"
    if layer == "entrypoint":
        return owner, "entrypoint"
    if owner == "moex_sentinel" and layer == "portfolio_snapshot_worker":
        return owner, "worker_entrypoint"
    if layer in {
        "domain",
        "services",
        "usecases",
        "runtime",
        "storage",
        "adapters",
        "views",
        "api",
        "composition",
        "migrations",
    }:
        return owner, layer
    if layer == "config":
        return owner, "config"
    return owner, "unknown"


def _module_name(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(parts)


def _module_index(source_root: Path) -> set[str]:
    return {_module_name(path, source_root) for path in source_root.rglob("*.py")}


def _import_targets(node: ast.Import | ast.ImportFrom, importer: str, modules: set[str]) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if node.level:
        package = importer.split(".")[:-1]
        ascend = node.level - 1
        if ascend >= len(package):
            return set()
        base = ".".join(package[: len(package) - ascend])
        if node.module:
            base = f"{base}.{node.module}" if base else node.module
    else:
        base = node.module or ""
    targets = {base} if base else set()
    for alias in node.names:
        if alias.name == "*" or not base:
            continue
        candidate = f"{base}.{alias.name}"
        if candidate in modules:
            targets.add(candidate)
    return targets


def _external_allowed(owner: str, layer: str, target: str) -> bool:
    top = target.split(".", 1)[0]
    if top in sys.stdlib_module_names or top in PURE_EXTERNALS:
        return True
    if layer in BUSINESS_LAYERS or layer == "shared":
        return False
    if layer == "adapters":
        return top in TRANSPORT_EXTERNALS
    if owner == "market_analytics":
        if layer == "analytics_transport":
            return top in {"httpx", "fastapi"}
        if layer == "entrypoint":
            return top == "uvicorn"
        return False
    return layer in {
        "adapters",
        "storage",
        "api",
        "views",
        "composition",
        "entrypoint",
        "config",
        "migrations",
        "worker_entrypoint",
        "analytics_transport",
    }


def _allowed_internal(owner: str, layer: str, target: str) -> bool:
    target_owner, target_layer = _layer(target)
    if target_owner is None:
        return True
    if target_owner == "sentinel_contracts":
        return True
    if target_owner != owner:
        if owner == "trading_automaton" and layer in {"adapters", "composition"}:
            return target_owner == "moex_sentinel" and target_layer == "adapters"
        return False
    if layer == "shared":
        return target_layer == "shared"
    if owner == "trading_automaton" and layer == "adapters" and target_owner == "moex_sentinel":
        return target_layer == "adapters"
    return target_layer in LAYER_RULES.get((owner, layer), set())


def _dependency_violations(importer: str, tree: ast.Module, modules: set[str]) -> list[str]:
    owner, layer = _layer(importer)
    if owner is None:
        return []
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in _import_targets(node, importer, modules):
            target_owner, _ = _layer(target)
            allowed = (
                _allowed_internal(owner, layer, target)
                if target_owner is not None
                else _external_allowed(owner, layer, target)
            )
            if not allowed:
                violations.append(f"{importer}:{node.lineno} imports {target}")
    return violations


def test_source_imports_follow_layer_dependency_directions() -> None:
    source_root = PROJECT_ROOT / "src"
    modules = _module_index(source_root)
    violations = []
    for path in source_root.rglob("*.py"):
        module = _module_name(path, source_root)
        if path.name == "__init__.py":
            module = f"{module}.__init__"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_dependency_violations(module, tree, modules))
    assert violations == [], "\n".join(violations)


@pytest.mark.parametrize(
    ("importer", "source", "forbidden_target"),
    [
        ("moex_sentinel.services.new_service", "import sqlalchemy", "sqlalchemy"),
        (
            "trading_automaton.domain.new_domain",
            "from moex_sentinel.storage import repositories",
            "moex_sentinel.storage",
        ),
        ("sentinel_contracts.new_contract", "import trading_automaton.domain", "trading_automaton"),
        ("market_analytics.service", "import httpx", "httpx"),
        ("market_analytics.indicators", "import sqlalchemy", "sqlalchemy"),
        ("market_analytics.app", "import t_tech", "t_tech"),
        ("market_analytics.app", "import sqlalchemy", "sqlalchemy"),
        ("market_analytics.__main__", "import t_tech", "t_tech"),
        ("market_analytics.__main__", "import sqlalchemy", "sqlalchemy"),
        ("moex_sentinel.services.new_service", "from .. import storage", "moex_sentinel.storage"),
        (
            "moex_sentinel.usecases.new_usecase",
            "from moex_sentinel import storage as repositories",
            "moex_sentinel.storage",
        ),
        ("moex_sentinel.domain.__init__", "from ..storage import repositories", "moex_sentinel.storage"),
        ("moex_sentinel.domain.new_domain", "from moex_sentinel.services import ports", "moex_sentinel.services"),
        ("trading_automaton.usecases.new_usecase", "import moex_sentinel.services", "moex_sentinel"),
    ],
)
def test_import_fixtures_reject_forbidden_dependency_directions(importer, source, forbidden_target) -> None:
    violations = _dependency_violations(importer, ast.parse(source), FIXTURE_MODULES)
    assert any(forbidden_target in item for item in violations), (importer, source, violations)


@pytest.mark.parametrize(
    ("importer", "source"),
    [
        ("moex_sentinel.services.new_service", "from . import ports"),
        ("moex_sentinel.services.new_service", "from ..domain import brokers"),
        ("moex_sentinel.storage.repositories.new_repository", "from ...domain import brokers"),
        ("trading_automaton.adapters.new_adapter", "from trading_automaton.domain import ports"),
        ("trading_automaton.adapters.new_adapter", "import moex_sentinel.adapters.registry"),
        ("trading_automaton.adapters.new_adapter", "import t_tech"),
        ("trading_automaton.composition", "from moex_sentinel.adapters import registry"),
        ("moex_sentinel.domain.new_domain", "from sentinel_contracts import base"),
    ],
)
def test_import_fixtures_allow_intended_layer_dependencies(importer, source) -> None:
    modules = FIXTURE_MODULES | {"moex_sentinel.services.ports", "trading_automaton.composition"}
    assert _dependency_violations(importer, ast.parse(source), modules) == [], (importer, source)
