import re
from pathlib import Path

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
    assert violations == []
