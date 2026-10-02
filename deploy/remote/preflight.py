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
from urllib.parse import urlsplit

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
    for name in (("trading-automaton", "portfolio-snapshot-worker") if contour == "TEST" else ("trading-automaton",)):
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
    if contour == "TEST":
        if cookie != "__Host-moex-session":
            fail("Sandbox requires its separate session cookie.")
        if config.get("name") != "moex-sentinel-remote":
            fail("TEST requires its retained Compose project.")
        default_network = config.get("networks", {}).get("default", {})
        if default_network.get("name") != "moex-sentinel-remote_default" or default_network.get("external"):
            fail("TEST requires its retained private application network.")
        for name in ("postgres-data", "automaton-data"):
            volume = config.get("volumes", {}).get(name, {})
            if not volume.get("external") or volume.get("name") != f"moex-sentinel-remote-{name}":
                fail("TEST must retain its existing durable volume identities.")
    if contour == "PROD":
        if mode != "READ_ONLY":
            fail("PROD TRADE is unavailable until P3 admission is implemented.")
        if config.get("name") != "moex-sentinel-prod" or cookie != "__Host-moex-prod-session":
            fail("PROD requires its separate project and session cookie.")
        if any(name in services for name in ("database", "migrations", "portfolio-snapshot-worker")):
            fail("PROD must not duplicate the TEST database, migrations or collector.")
        if "postgres-data" in config.get("volumes", {}):
            fail("PROD must not define a PostgreSQL volume.")
        networks = config.get("networks", {})
        if (
            set(networks) != {"default", "data"}
            or networks["default"].get("name") != "moex-sentinel-prod_default"
            or networks["default"].get("external")
            or networks["data"].get("name") != "moex-sentinel-data"
            or not networks["data"].get("external")
        ):
            fail("PROD requires its own app network and the shared external data network.")
        try:
            database = urlsplit(backend.get("DATABASE_URL", ""))
            if (
                database.scheme != "postgresql+psycopg"
                or database.hostname != "sentinel-shared-postgres"
                or database.port != 5432
                or database.path != "/moex_sentinel"
            ):
                fail("PROD must use the reviewed shared PostgreSQL target and database.")
        except ValueError:
            fail("Invalid shared PostgreSQL URL.")
        volume = config.get("volumes", {}).get("automaton-data", {})
        if not volume.get("external") or volume.get("name") != "moex-sentinel-prod-automaton-data":
            fail("PROD Worker volume must remain external and separate from TEST.")
        ports = services["frontend"].get("ports", [])
        if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1" or str(ports[0].get("published")) != "8081":
            fail("PROD UI requires the reviewed loopback port 8081.")
        for name, service in services.items():
            if name != "frontend" and service.get("ports"):
                fail("Internal services must not publish ports.")


def validate_shared_database_pair(test: dict, prod: dict) -> None:
    validate_contour_config(test)
    validate_contour_config(prod)
    owners = test["services"]
    if owners["backend"]["environment"].get("APPLICATION_ENVIRONMENT") != "TEST":
        fail("Shared database owner must be TEST.")
    if sum(service.get("image", "").startswith("postgres:") for service in owners.values()) != 1:
        fail("Exactly one TEST PostgreSQL service is required.")
    database = owners.get("database", {})
    if database.get("ports") or database.get("networks", {}).get("data", {}).get("aliases") != [
        "sentinel-shared-postgres"
    ]:
        fail("Shared PostgreSQL must be private with its reviewed unique data-network alias.")
    if test.get("networks", {}).get("data", {}) != prod.get("networks", {}).get("data", {}):
        fail("Both contours must use the same external data network.")
    for name in ("backend", "migrations", "portfolio-snapshot-worker"):
        if "data" not in owners[name].get("networks", {}):
            fail("TEST database clients must join the shared data network.")
    if set(prod["services"]["backend"].get("networks", {})) != {"default", "data"}:
        fail("PROD Core must join only its app and shared data networks.")
    expected = owners["backend"]["environment"].get("DATABASE_URL")
    for service in (owners["migrations"], owners["portfolio-snapshot-worker"], prod["services"]["backend"]):
        if service.get("environment", {}).get("DATABASE_URL") != expected:
            fail("Core, migrations and collector must use the exact same shared database URL.")
    if owners["portfolio-snapshot-worker"]["environment"].get("PORTFOLIO_SNAPSHOT_ALL_ENVIRONMENTS") != "true":
        fail("The single TEST collector must include both contours.")
    if not test.get("volumes", {}).get("postgres-data", {}).get("external"):
        fail("Existing TEST PostgreSQL volume must remain external.")


def main() -> None:
    directory = Path(__file__).resolve().parent
    default_env = ".env.production" if os.environ.get("REMOTE_CONTOUR") == "PROD" else ".env"
    env_path = Path(os.environ.get("REMOTE_ENV_FILE", str(directory / default_env))).resolve()
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
                str(
                    directory
                    / ("compose.production.sh" if os.environ.get("REMOTE_CONTOUR") == "PROD" else "compose.sh")
                ),
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
    password = (
        services["database"]["environment"]["POSTGRES_PASSWORD"]
        if "database" in services
        else urlsplit(services["backend"]["environment"]["DATABASE_URL"]).password or ""
    )
    if not re.fullmatch(r"[a-fA-F0-9]{64,}", password):
        fail("Use at least 32 random bytes encoded as hex for POSTGRES_PASSWORD.")
    for name in set(services) - {"database", "python-base"}:
        image = services[name].get("image", "")
        if "replace-" in image or image.endswith((":latest", ":local")):
            fail("Set RELEASE_TAG to a unique reviewed release identifier.")
    volumes = config.get("volumes", {})
    if not all(
        volumes.get(name, {}).get("external")
        for name in (("postgres-data", "automaton-data") if "database" in services else ("automaton-data",))
    ):
        fail("Both durable volumes must remain external.")
    if services["backend"]["environment"]["APPLICATION_ENVIRONMENT"] == "PROD":
        owner_env = os.environ.get("TEST_REMOTE_ENV_FILE")
        if not owner_env:
            fail("PROD preflight requires TEST_REMOTE_ENV_FILE to validate the shared database owner.")
        try:
            owner_environment = os.environ.copy()
            owner_environment.update(REMOTE_ENV_FILE=owner_env, REMOTE_CONTOUR="TEST")
            owner = subprocess.run(
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
                env=owner_environment,
                capture_output=True,
                text=True,
                check=True,
            )
            validate_shared_database_pair(json.loads(owner.stdout), config)
        except (OSError, subprocess.CalledProcessError, ValueError):
            fail("Unable to validate TEST shared database configuration.")
    print(  # noqa: T201 - CLI status output contains no configuration values.
        "PASS: Compose parses; internal ports closed; UI loopback-only; "
        "Worker opt-in singleton; durable volumes external."
    )
    print(  # noqa: T201 - CLI status output contains no configuration values.
        "Not checked: daemon/host capacity, backup restore, broker connectivity, "
        "authentication outside SSH, runtime health, external data network internal flag."
    )


if __name__ == "__main__":
    main()
