"""External HTTPS smoke check; does not print or persist credentials."""

import json
import subprocess
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

base_url = "https://135.136.178.252"
opener = build_opener(HTTPCookieProcessor(CookieJar()))


def call(path: str, method: str = "GET", body: dict[str, str] | None = None, csrf: str | None = None):
    headers = {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    request = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        response = opener.open(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


def expect(label: str, actual: int, wanted: int) -> None:
    if actual != wanted:
        raise RuntimeError(f"{label}: expected HTTP {wanted}, received {actual}")


result = subprocess.run(
    [
        "ssh",
        "-i",
        "/Users/artemivanov/.ssh/id_rsa",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=8",
        "codex-deploy@135.136.178.252",
        "sudo -n cat /root/moex-sentinel-operator-password",
    ],
    capture_output=True,
    text=True,
    check=False,
)
if result.returncode or not result.stdout.strip():
    raise RuntimeError("Could not read the one-time operator password over SSH")
password = result.stdout.strip()

expect("UI", call("/")[0], 200)
expect("health", call("/api/health")[0], 200)
expect("internal denied", call("/internal/")[0], 404)
expect("anonymous API", call("/api/brokers")[0], 401)
expect("anonymous session", call("/api/auth/session")[0], 401)

status, headers, content = call("/api/auth/login", "POST", {"username": "operator", "password": password})
expect("login", status, 200)
cookie = headers.get("Set-Cookie", "")
if not all(flag in cookie for flag in ("__Host-moex-session=", "Secure", "HttpOnly", "SameSite=strict")):
    raise RuntimeError("Secure session cookie flags are incomplete")
csrf = json.loads(content)["csrf_token"]
expect("session", call("/api/auth/session")[0], 200)
expect("CSRF denied", call("/api/auth/logout", "POST")[0], 403)
expect("logout", call("/api/auth/logout", "POST", csrf=csrf)[0], 204)
expect("revoked session", call("/api/auth/session")[0], 401)

statuses = [call("/api/auth/login")[0] for _ in range(20)]
if 429 not in statuses:
    raise RuntimeError("Login rate limit did not return HTTP 429")
print("PASS: HTTPS UI, health, API auth, secure cookie, CSRF, logout/revoke, internal deny, login limit")
