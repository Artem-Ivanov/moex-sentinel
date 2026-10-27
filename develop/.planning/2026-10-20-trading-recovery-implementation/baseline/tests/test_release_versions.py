"""Offline release metadata gate, without importing application dependencies."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sentinel_contracts.release import ReleaseVersionError, validate_release


def test_release_guard_accepts_current_independent_packages():
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "src/sentinel_contracts/release.py"), str(root)],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def release_tree(tmp_path):
    root = Path(__file__).parents[1]
    for relative in (
        "pyproject.toml",
        "uv.lock",
        "src/sentinel_contracts/version.py",
        "src/moex_sentinel/__init__.py",
        "frontend/package.json",
        "frontend/package-lock.json",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    return tmp_path


@pytest.mark.parametrize(
    "source",
    [
        "pyproject.toml",
        "uv.lock",
        "src/sentinel_contracts/version.py",
        "src/moex_sentinel/__init__.py",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend-lock-root",
    ],
)
def test_each_mismatch_blocks_release_then_restoration_passes(release_tree, source):
    relative = "frontend/package-lock.json" if source == "frontend-lock-root" else source
    path = release_tree / relative
    original = path.read_text()
    if source == "src/moex_sentinel/__init__.py":
        path.write_text('__version__ = "0.2.0"\n')
    elif source.startswith("frontend"):
        data = json.loads(original)
        if source == "frontend-lock-root":
            data["packages"][""]["version"] = "9.9.9"
        else:
            data["version"] = "9.9.9"
        path.write_text(json.dumps(data))
    elif source == "uv.lock":
        path.write_text(
            original.replace('name = "moex-sentinel"\nversion = "0.2.0"', 'name = "moex-sentinel"\nversion = "9.9.9"')
        )
    else:
        path.write_text(original.replace('"0.2.0"', '"9.9.9"', 1))
    with pytest.raises(ReleaseVersionError):
        validate_release(release_tree)
    path.write_text(original)
    validate_release(release_tree)


@pytest.mark.parametrize("change", ["missing", "duplicate"])
def test_missing_duplicate_local_lock_entry_blocks_release(release_tree, change):
    path = release_tree / "uv.lock"
    original = path.read_text()
    if change == "missing":
        path.write_text(original.replace('name = "moex-sentinel"', 'name = "other-local"'))
    else:
        path.write_text(
            original + '\n[[package]]\nname = "moex-sentinel"\nversion = "0.2.0"\nsource = { editable = "." }\n'
        )
    with pytest.raises(ReleaseVersionError):
        validate_release(release_tree)


def test_frontend_may_have_distinct_version(release_tree):
    for name in ("package.json", "package-lock.json"):
        path = release_tree / "frontend" / name
        data = json.loads(path.read_text())
        data["version"] = "3.4.5"
        if name == "package-lock.json":
            data["packages"][""]["version"] = "3.4.5"
        path.write_text(json.dumps(data))
    validate_release(release_tree)


def test_python_gate_does_not_require_frontend_files(release_tree):
    shutil.rmtree(release_tree / "frontend")
    validate_release(release_tree, python_only=True)


@pytest.mark.parametrize("version", ["invalid", True, "01.2.3", "1.2.3" + "0" * 32])
def test_equal_but_malformed_frontend_versions_block_release(release_tree, version):
    for name in ("package.json", "package-lock.json"):
        path = release_tree / "frontend" / name
        data = json.loads(path.read_text())
        data["version"] = version
        if name == "package-lock.json":
            data["packages"][""]["version"] = version
        path.write_text(json.dumps(data))
    with pytest.raises(ReleaseVersionError):
        validate_release(release_tree)


def test_python_docker_equivalent_gate_runs_without_application_imports(release_tree):
    shutil.rmtree(release_tree / "frontend")
    shutil.copyfile(
        Path(__file__).parents[1] / "src/sentinel_contracts/release.py",
        release_tree / "src/sentinel_contracts/release.py",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(release_tree / "src/sentinel_contracts/release.py"),
            str(release_tree),
            "--python-only",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "addition",
    [
        'SERVICE_VERSION = "0.3.0".strip()\n',
        'SERVICE_VERSION += ".extra"\n',
        'if True:\n    SERVICE_VERSION = "0.3.0"\n',
        'SERVICE_VERSION: str = "0.3.0"\n',
        'SERVICE_VERSION = "0.3.0"\n',
        'raise RuntimeError("version module side effect")\n',
    ],
)
def test_additional_version_binding_or_execution_blocks_release_then_restoration(release_tree, addition):
    path = release_tree / "src/sentinel_contracts/version.py"
    original = path.read_text()
    path.write_text(original + "\n" + addition)
    with pytest.raises(ReleaseVersionError):
        validate_release(release_tree)
    path.write_text(original)
    validate_release(release_tree)


@pytest.mark.parametrize(
    "addition",
    [
        "from math import pi as __version__\n",
        "import math as __version__\n",
        "if True:\n    from math import pi as __version__\n",
        '__version__ += ".extra"\n',
    ],
)
def test_additional_core_alias_binding_blocks_release_then_restoration(release_tree, addition):
    path = release_tree / "src/moex_sentinel/__init__.py"
    original = path.read_text()
    path.write_text(original + "\n" + addition)
    with pytest.raises(ReleaseVersionError):
        validate_release(release_tree)
    path.write_text(original)
    validate_release(release_tree)
