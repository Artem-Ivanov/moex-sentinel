#!/usr/bin/env python3
"""Read-only remote Compose safety checks; never print resolved environment."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

PASSWORD_HASH_PATTERN = re.compile(r"scrypt:16384:8:5:[0-9a-fA-F]{32}:[0-9a-fA-F]{64}\Z")


def fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def validate_auth_config(environment: dict[str, str]) -> None:
    if environment.get("AUTH_INSECURE_LOOPBACK") != "false":
        fail("Remote deployment requires AUTH_INSECURE_LOOPBACK=false.")
    password_hash = environment.get("AUTH_PASSWORD_HASH", "")
    if not PASSWORD_HASH_PATTERN.fullmatch(password_hash):
        fail("Set AUTH_PASSWORD_HASH to a generated scrypt verifier.")


def validate_contour_config(config: dict) -> None:
    services = config["services"]
    backend = services["backend"].get("environment", {})
    contour = backend.get("APPLICATION_ENVIRONMENT")
    mode = backend.get("BROKER_ACCESS_MODE")
    if contour not in {"TEST", "PROD"} or mode not in {"READ_ONLY", "TRADE"}:
        fail("Explicit valid APPLICATION_ENVIRONMENT and BROKER_ACCESS_MODE are required.")
    for name in ("trading-automaton", "portfolio-snapshot-worker"):
        environment = services[name].get("environment", {})
        if environment.get("APPLICATION_ENVIRONMENT") != contour or environment.get("BROKER_ACCESS_MODE") != mode:
            fail("Broker owners must use identical contour and access mode.")
    if mode == "READ_ONLY" and services["trading-automaton"]["environment"].get("STRATEGY_ENABLED") != "false":
        fail("READ_ONLY requires STRATEGY_ENABLED=false.")
    cookie = backend.get("AUTH_SESSION_COOKIE_NAME", "")
    origin = backend.get("AUTH_ALLOWED_ORIGIN", "")
    if not re.fullmatch(r"__Host-[A-Za-z0-9_-]+", cookie):
        fail("Remote auth requires a safe __Host- session cookie name.")
    expected_origin = "https://135.136.178.252" + (":8443" if contour == "PROD" else "")
    if origin != expected_origin:
        fail("Origin must match the reviewed contour HTTPS listener.")
    if contour == "TEST" and cookie != "__Host-moex-session":
        fail("Sandbox requires its separate session cookie.")
    if contour == "PROD":
        if mode != "READ_ONLY":
            fail("PROD TRADE is unavailable until P3 admission is implemented.")
        if config.get("name") != "moex-sentinel-prod" or cookie != "__Host-moex-prod-session":
            fail("PROD requires its separate project and session cookie.")
        networks = config.get("networks", {})
        if (
            set(networks) != {"default"}
            or networks["default"].get("name") != "moex-sentinel-prod_default"
            or networks["default"].get("external")
        ):
            fail("PROD requires its own private Compose network.")
        for name, suffix in (("postgres-data", "postgres-data"), ("automaton-data", "automaton-data")):
            volume = config.get("volumes", {}).get(name, {})
            if not volume.get("external") or volume.get("name") != f"moex-sentinel-prod-{suffix}":
                fail("PROD durable volumes must be external and separate from Sandbox.")
        ports = services["frontend"].get("ports", [])
        if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1" or str(ports[0].get("published")) != "8081":
            fail("PROD UI requires the reviewed loopback port 8081.")
        for name, service in services.items():
            if name != "frontend" and service.get("ports"):
                fail("Internal services must not publish ports.")


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
    validate_auth_config(services["backend"].get("environment", {}))
    validate_contour_config(config)
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
