#!/usr/bin/env python3
"""Read-only remote Compose safety checks; never print resolved environment."""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def main() -> None:
    directory = Path(__file__).resolve().parent
    env_path = Path(os.environ.get("REMOTE_ENV_FILE", str(directory / ".env"))).resolve()
    if not env_path.is_file():
        fail("Create deploy/remote/.env from .env.example, then chmod 600.")
    if stat.S_IMODE(env_path.stat().st_mode) & 0o077:
        fail("Environment file must not be readable by group/others (chmod 600).")
    docker = shutil.which("docker")
    shell = shutil.which("sh")
    if docker is None or shell is None:
        fail("Docker and a POSIX shell are required.")
    try:
        version = subprocess.run([docker, "compose", "version", "--short"], capture_output=True, text=True, check=True)
        parts = re.search(r"(\d+)\.(\d+)\.(\d+)", version.stdout)
        if not parts or tuple(map(int, parts.groups())) < (2, 24, 4):
            fail("Docker Compose >= 2.24.4 is required.")
        result = subprocess.run(
            [
                shell,
                str(directory / "compose.sh"),
                "--profile",
                "trading",
                "--profile",
                "migrations",
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        config = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, ValueError):
        fail("Compose configuration failed; check required env names and Docker Compose installation.")
    services = config["services"]
    for name, service in services.items():
        if name != "frontend" and service.get("ports"):
            fail(f"Internal service {name} must not publish host ports.")
    ports = services["frontend"].get("ports", [])
    if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1" or ports[0].get("target") != 8080:
        fail("Frontend must publish only 127.0.0.1 -> 8080.")
    worker = services["trading-automaton"]
    if "trading" not in worker.get("profiles", []) or worker.get("scale") != 1:
        fail("Worker requires explicit trading profile and scale=1.")
    password = services["database"]["environment"]["POSTGRES_PASSWORD"]
    if not re.fullmatch(r"[a-fA-F0-9]{64,}", password):
        fail("Use at least 32 random bytes encoded as hex for POSTGRES_PASSWORD.")
    for name in ("backend", "migrations", "analytics", "trading-automaton", "frontend", "portfolio-snapshot-worker"):
        image = services[name].get("image", "")
        if "replace-" in image or image.endswith((":latest", ":local")):
            fail("Set RELEASE_TAG to a unique reviewed release identifier.")
    volumes = config.get("volumes", {})
    if not all(volumes.get(name, {}).get("external") for name in ("postgres-data", "automaton-data")):
        fail("Both durable volumes must remain external.")
    print(  # noqa: T201 - CLI status output contains no configuration values.
        "PASS: Compose parses; internal ports closed; UI loopback-only; "
        "Worker opt-in singleton; durable volumes external."
    )
    print(  # noqa: T201 - CLI status output contains no configuration values.
        "Not checked: daemon/host capacity, backup restore, broker connectivity, "
        "authentication outside SSH, runtime health."
    )


if __name__ == "__main__":
    main()
