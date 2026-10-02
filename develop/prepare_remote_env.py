"""One-time VPS setup: write private Compose env and operator password."""

import hashlib
import os
import secrets
from pathlib import Path

env_path = Path("/etc/moex-sentinel/remote.env")
template_path = Path("/opt/moex-sentinel/current/deploy/remote/.env.example")
password_path = Path("/root/moex-sentinel-operator-password")
if env_path.exists() or password_path.exists():
    raise SystemExit("Refusing to replace existing credentials")

password = secrets.token_urlsafe(24)
salt = secrets.token_bytes(16)
digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=5, dklen=32)
settings = {
    "RELEASE_TAG": "vps-20261001-auth-tls-r1",
    "POSTGRES_PASSWORD": secrets.token_hex(32),
    "AUTH_USERNAME": "operator",
    "AUTH_PASSWORD_HASH": f"scrypt:16384:8:5:{salt.hex()}:{digest.hex()}",
    "STRATEGY_ENABLED": "false",
}
template = template_path.read_text(encoding="utf-8")
lines = []
found = set()
for line in template.splitlines():
    key, separator, _ = line.partition("=")
    if separator and key in settings:
        found.add(key)
    lines.append(f"{key}={settings[key]}" if separator and key in settings else line)
if missing := settings.keys() - found:
    raise SystemExit(f"Missing template keys: {', '.join(sorted(missing))}")

os.umask(0o077)
env_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
with password_path.open("x", encoding="utf-8") as stream:
    stream.write(password + "\n")
created_env = False
try:
    with env_path.open("x", encoding="utf-8") as stream:
        created_env = True
        stream.write("\n".join(lines) + "\n")
except Exception:
    if created_env:
        env_path.unlink(missing_ok=True)
    password_path.unlink()
    raise
print("Created root-only operator password file and Compose environment; trading disabled.")
