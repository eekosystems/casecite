#!/usr/bin/env python3
"""
Production-mode smoke test for CaseCite

Boots the real backend with DEBUG off against a real Postgres and Redis, walks
the first-run path (register the admin, read the admin-only system endpoints),
then RESTARTS the process and signs in again. The unit tests run with
DEBUG=true on SQLite, so nothing else exercises the fail-closed production
branches or a second boot over the data the first one wrote.

Expects a FRESH database (the first registered account becomes the admin) and
the production environment already set: DATABASE_URL, REDIS_URL, SECRET_KEY,
ENCRYPTION_SALT, AUDIT_HMAC_KEY, CORS_ORIGINS, ALLOWED_HOSTS,
REGISTRATION_BOOTSTRAP_TOKEN (production refuses to create the first account
without it), DISK_ENCRYPTION_ACKNOWLEDGED=true and one provider key.

Usage:
    python scripts/smoke_production.py [--port 8765]
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
API = "/api/v1"
STARTUP_TIMEOUT_SECONDS = 180
ADMIN_EMAIL = "smoke-admin@example.com"
ADMIN_PASSWORD = "Smoke-Test-Passw0rd!"  # nosec B105 - throwaway account on a throwaway database


class SmokeFailure(Exception):
    """A step did not behave as a healthy production instance would."""


class Server:
    """One uvicorn process, with its output captured for the failure report."""

    def __init__(self, port: int):
        self.base_url = f"http://127.0.0.1:{port}"
        self.log_path = Path(tempfile.gettempdir()) / f"casecite-smoke-{os.getpid()}.log"
        self._proc = None
        self._port = port

    def start(self) -> None:
        # The child inherits its own handle to the log, so ours can close at once.
        with open(self.log_path, "ab") as log:
            self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self._port),
                ],
                cwd=BACKEND_DIR,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise SmokeFailure(f"backend exited during startup (code {self._proc.returncode})")
            try:
                if httpx.get(f"{self.base_url}/health", timeout=5).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise SmokeFailure(f"backend not healthy after {STARTUP_TIMEOUT_SECONDS}s")

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()

    def log_tail(self, lines: int = 60) -> str:
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return "(no server log)"
        return "\n".join(text.splitlines()[-lines:])


def expect(response: httpx.Response, status: int, step: str) -> httpx.Response:
    if response.status_code != status:
        raise SmokeFailure(
            f"{step}: expected HTTP {status}, got {response.status_code}: {response.text[:300]}"
        )
    print(f"  ok  {step}")
    return response


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def first_boot(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=60) as client:
        expect(client.get("/ready"), 200, "GET /ready")

        csrf = expect(client.get(f"{API}/csrf-token"), 200, "GET csrf-token").json()["csrf_token"]
        # Production cookies are always Secure, and this test talks plain HTTP
        # to loopback, so httpx would not send the CSRF cookie back. Set it by
        # hand: the double-submit check needs the cookie and the header to
        # match. (Authentication below uses the bearer token, not cookies.)
        client.cookies.set("_csrf", csrf)
        account = {"email": ADMIN_EMAIL, "name": "Smoke Admin", "password": ADMIN_PASSWORD}
        expect(
            client.post(f"{API}/auth/register", json=account, headers={"X-CSRF-Token": csrf}),
            403,
            "first account without the bootstrap token is refused",
        )
        registered = expect(
            client.post(
                f"{API}/auth/register",
                json={**account, "bootstrap_token": os.environ["REGISTRATION_BOOTSTRAP_TOKEN"]},
                headers={"X-CSRF-Token": csrf},
            ),
            201,
            "register first account",
        ).json()
        if "admin" not in registered["user"]["roles"]:
            raise SmokeFailure("first account is not an admin — the database was not fresh")
        auth = bearer(registered["access_token"])

        expect(client.get(f"{API}/auth/me", headers=auth), 200, "GET auth/me")
        health = expect(client.get("/health", headers=auth), 200, "GET /health as admin").json()
        if not health.get("components"):
            raise SmokeFailure("GET /health as admin: no component details returned")
        expect(client.get("/metrics", headers=auth), 200, "GET /metrics as admin")
        expect(client.get("/metrics"), 401, "GET /metrics anonymously is refused")
        expect(
            client.post(f"{API}/auth/logout", headers={**auth, "X-CSRF-Token": csrf}),
            200,
            "logout",
        )


def second_boot(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=60) as client:
        token = expect(
            client.post(
                f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
            ),
            200,
            "login after restart",
        ).json()["access_token"]
        verdict = expect(
            client.get(f"{API}/admin/audit/verify", headers=bearer(token)),
            200,
            "GET admin/audit/verify",
        ).json()
        if not verdict["valid"]:
            raise SmokeFailure(
                f"audit chain does not verify ({verdict['entries_checked']} entries, "
                f"first invalid: {verdict['first_invalid_id']})"
            )
        print(f"  ok  audit chain verifies ({verdict['entries_checked']} entries)")


def main() -> int:
    parser = argparse.ArgumentParser(description="CaseCite production-mode smoke test")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--phase",
        choices=("all", "first", "second"),
        default="all",
        help="'first' boots a fresh instance and registers the admin; 'second' boots over "
        "existing data and signs in (the backup-and-restore drill runs them around a "
        "backup, a wipe and a restore); 'all' runs both back to back",
    )
    args = parser.parse_args()

    if os.environ.get("DEBUG", "").lower() in ("true", "1", "yes"):
        print("DEBUG is set — this test only means something in production mode.")
        return 2
    if not os.environ.get("REGISTRATION_BOOTSTRAP_TOKEN"):
        print("REGISTRATION_BOOTSTRAP_TOKEN is not set — the first account cannot be created.")
        return 2

    server = Server(args.port)
    try:
        if args.phase in ("all", "first"):
            print("First boot")
            server.start()
            first_boot(server.base_url)
            server.stop()

        if args.phase in ("all", "second"):
            print("Second boot (restart over the first boot's data)")
            server.start()
            second_boot(server.base_url)
    except SmokeFailure as failure:
        print(f"\nFAILED  {failure}\n\n--- backend log (tail) ---\n{server.log_tail()}")
        return 1
    finally:
        server.stop()

    print("\nProduction smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
