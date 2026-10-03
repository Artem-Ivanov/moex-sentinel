"""Stdlib metadata gate for Python and frontend packages before release."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path


class ReleaseVersionError(ValueError):
    """Release package metadata is missing, malformed or inconsistent."""


def validate_release(root: Path, *, python_only: bool = False) -> None:
    """Validate literals without importing application code or its dependencies."""
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        packages = tomllib.loads((root / "uv.lock").read_text())["package"]
        local = [package for package in packages if package.get("name") == project["name"]]
        if len(local) != 1 or local[0].get("source") != {"editable": "."}:
            raise ReleaseVersionError("Python lock must contain exactly one editable local package.")
        tree = ast.parse((root / "src/sentinel_contracts/version.py").read_text())
        statements = tree.body[int(ast.get_docstring(tree) is not None) :]
        if (
            len(statements) != 1
            or not isinstance(statements[0], ast.Assign)
            or len(statements[0].targets) != 1
            or not isinstance(statements[0].targets[0], ast.Name)
            or statements[0].targets[0].id != "SERVICE_VERSION"
            or not isinstance(statements[0].value, ast.Constant)
            or not isinstance(statements[0].value.value, str)
        ):
            raise ReleaseVersionError("Shared version module must contain only one literal assignment.")
        version = statements[0].value.value
        if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
            raise ReleaseVersionError("Shared version must be one safe literal.")
        if len(version) > 32 or project["version"] != local[0]["version"] or project["version"] != version:
            raise ReleaseVersionError("Python release versions disagree.")
        alias = ast.parse((root / "src/moex_sentinel/__init__.py").read_text())
        statements = alias.body[int(ast.get_docstring(alias) is not None) :]
        if (
            len(statements) not in {1, 2}
            or not isinstance(statements[0], ast.ImportFrom)
            or statements[0].module != "sentinel_contracts.version"
            or statements[0].level != 0
            or len(statements[0].names) != 1
            or statements[0].names[0].name != "SERVICE_VERSION"
            or statements[0].names[0].asname != "__version__"
        ):
            raise ReleaseVersionError("Core version must contain only its shared alias and optional __all__.")
        if len(statements) == 2:
            exports = statements[1]
            if (
                not isinstance(exports, ast.Assign)
                or len(exports.targets) != 1
                or not isinstance(exports.targets[0], ast.Name)
                or exports.targets[0].id != "__all__"
                or not isinstance(exports.value, ast.List)
                or len(exports.value.elts) != 1
                or not isinstance(exports.value.elts[0], ast.Constant)
                or exports.value.elts[0].value != "__version__"
            ):
                raise ReleaseVersionError("Core optional __all__ must contain only its version alias.")
        if not python_only:
            package = json.loads((root / "frontend/package.json").read_text())
            lock = json.loads((root / "frontend/package-lock.json").read_text())
            version = package["version"]
            if (
                not isinstance(version, str)
                or len(version) > 32
                or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
            ):
                raise ReleaseVersionError("Frontend version must be safe MAJOR.MINOR.PATCH.")
            if package["version"] != lock["version"] or package["version"] != lock["packages"][""]["version"]:
                raise ReleaseVersionError("Frontend release versions disagree.")
    except (OSError, KeyError, TypeError, SyntaxError, ValueError) as error:
        if isinstance(error, ReleaseVersionError):
            raise
        raise ReleaseVersionError("Release metadata is missing or malformed.") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--python-only", action="store_true")
    arguments = parser.parse_args()
    try:
        validate_release(arguments.root, python_only=arguments.python_only)
    except ReleaseVersionError as error:
        parser.exit(1, f"FAIL: {error}\n")
    sys.stdout.write("PASS: release metadata versions agree.\n")


if __name__ == "__main__":
    main()
