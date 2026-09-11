"""The profile runner owns its disposable database even when the workload fails."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "develop/scripts/profile_postgresql.sh"


@pytest.mark.parametrize(("failure", "exit_code"), [("", 0), ("benchmark", 7), ("start", 9)])
def test_runner_uses_only_owned_container_and_cleans_it_on_failure(tmp_path, failure, exit_code):
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    trace = tmp_path / "calls.jsonl"
    docker = bin_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['PROFILE_TEST_TRACE'], 'a') as stream:\n"
        "    stream.write(json.dumps(['docker', *sys.argv[1:]]) + '\\n')\n"
        "operation = sys.argv[1]\n"
        "if operation == 'create': print('synthetic-container')\n"
        "if operation == 'start' and os.environ['PROFILE_TEST_FAILURE'] == 'start': sys.exit(9)\n"
        "if operation == 'port': print('127.0.0.1:55449')\n",
        encoding="utf-8",
    )
    benchmark = bin_path / "benchmark-python"
    benchmark.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['PROFILE_TEST_TRACE'], 'a') as stream:\n"
        "    stream.write(json.dumps(['benchmark', *sys.argv[1:], os.environ['PG_PROFILE_DATABASE_URL']]) + '\\n')\n"
        "sys.exit(7 if os.environ['PROFILE_TEST_FAILURE'] == 'benchmark' else 0)\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    benchmark.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - execute the repository runner with synthetic tools
        ["bash", str(RUNNER), "--instruments", "6", "--iterations", "2"],  # noqa: S607
        env={
            **os.environ,
            "PATH": str(bin_path) + os.pathsep + os.environ["PATH"],
            "BENCHMARK_PYTHON": str(benchmark),
            "PROFILE_TEST_TRACE": str(trace),
            "PROFILE_TEST_FAILURE": failure,
            "PG_PROFILE_DATABASE_URL": "must-not-use-ambient-database",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == exit_code, result.stderr
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    create = calls[0]
    assert create[:2] == ["docker", "create"]
    assert create[create.index("--name") + 1].startswith("moex-sentinel-profile-")
    assert create[create.index("--publish") + 1] == "127.0.0.1::5432"
    assert create[create.index("--tmpfs") + 1] == "/var/lib/postgresql/data:rw"
    assert not {"--volume", "-v", "--mount"}.intersection(create)
    assert create[-1] == "postgres:16-alpine"
    assert calls[-2:] == [
        ["docker", "stop", "--time", "10", "synthetic-container"],
        ["docker", "rm", "synthetic-container"],
    ]
    benchmark_calls = [call for call in calls if call[0] == "benchmark"]
    if failure == "start":
        assert not benchmark_calls
    else:
        assert benchmark_calls == [
            [
                "benchmark",
                "-m",
                "develop.benchmarks.postgresql",
                "--instruments",
                "6",
                "--iterations",
                "2",
                "postgresql+psycopg://sentinel_profile@127.0.0.1:55449/sentinel_profile",
            ]
        ]
