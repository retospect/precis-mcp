"""End-to-end: the real ``precis users`` entrypoint against a live server.

Everything else stops at a seam — the CLI tests call the private helpers,
the gate tests use ``TestClient``. This one runs the actual
``precis.cli.main`` dispatch (``Store.connect`` → subcommand → ``close``)
and then talks to a real uvicorn over a real socket, which is the only
way to catch a break in the parts nothing else touches: argv wiring, the
DSN plumbing, and whether a browser actually receives a usable
``WWW-Authenticate`` challenge rather than a framework error page.

Slow by nature (a process spawn plus a bind), so it is one test, not a
suite.
"""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from precis.store import Store
from tests.conftest import _active_dsn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(url: str, *, login: str | None = None, password: str = "") -> tuple[int, str]:
    req = urllib.request.Request(url)
    if login is not None:
        raw = base64.b64encode(f"{login}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {raw}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code == 401:
            # The header a browser needs to show its login prompt.
            assert exc.headers.get("WWW-Authenticate", "").startswith("Basic realm=")
        return exc.code, body


def _post(
    url: str, form: dict[str, str], *, login: str, password: str
) -> tuple[int, str]:
    """POST a form without following the redirect — the 303 *is* the assertion."""
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    raw = base64.b64encode(f"{login}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {raw}")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _cli(*argv: str, dsn: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "precis", "users", "--database-url", dsn, *argv],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.slow
def test_cli_creates_an_account_a_live_server_then_accepts(store: Store) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")

    dsn = _active_dsn()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "PRECIS_DATABASE_URL": dsn,
        "PRECIS_EMBEDDER": "mock",
        "PRECIS_WEB_PORT": str(port),
    }
    env.pop("PRECIS_WEB_AUTH", None)  # served default = closed
    env.pop("PRECIS_WEB_PASSWORD_PEPPER", None)

    server = subprocess.Popen(
        [sys.executable, "-m", "precis", "web", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if server.poll() is not None:
                pytest.fail(f"precis web exited early:\n{server.stdout.read()}")  # type: ignore[union-attr]
            try:
                if _get(f"{base}/healthz")[0] == 200:
                    break
            except OSError:
                time.sleep(0.3)
        else:
            pytest.fail("precis web never became reachable")

        # Empty roster: the probe stays open, the UI explains itself.
        assert _get(f"{base}/healthz")[0] == 200
        status, body = _get(f"{base}/drive")
        assert status == 503
        assert "precis users add" in body

        # The real CLI, password on stdin, never argv.
        created = _cli(
            "add",
            "smoke",
            "--abbrev",
            "sk",
            "--password-stdin",
            "--no-pepper",
            dsn=dsn,
            stdin="hunter2-swordfish",
        )
        assert created.returncode == 0, created.stderr
        assert "created smoke (sk)" in created.stdout

        assert _get(f"{base}/drive")[0] == 401
        assert _get(f"{base}/drive", login="smoke", password="wrong")[0] == 401
        assert (
            _get(f"{base}/drive", login="smoke", password="hunter2-swordfish")[0] == 200
        )

        # Self-service password change, through the real server: the old
        # password must stop working on the very next request, not when
        # the gate's credential cache happens to expire.
        assert (
            _get(f"{base}/account", login="smoke", password="hunter2-swordfish")[0]
            == 200
        )
        status, _ = _post(
            f"{base}/account/password",
            {
                "current_password": "hunter2-swordfish",
                "new_password": "tinned-brass-lamp",
                "confirm_password": "tinned-brass-lamp",
            },
            login="smoke",
            password="hunter2-swordfish",
        )
        assert status == 303
        assert (
            _get(f"{base}/drive", login="smoke", password="hunter2-swordfish")[0] == 401
        )
        assert (
            _get(f"{base}/drive", login="smoke", password="tinned-brass-lamp")[0] == 200
        )

        listed = _cli("list", dsn=dsn)
        assert listed.returncode == 0
        assert "smoke" in listed.stdout and "active" in listed.stdout

        # Disabling the ONLY account leaves nobody who can log in, which
        # is the empty-roster situation again — so the operator gets the
        # 503 that names the fix, not a 401 they'd read as a typo. (With
        # a second account still enabled, the disabled one gets 401; see
        # test_auth.py::test_disabled_user_is_401.) A fresh password
        # sidesteps the gate's TTL cache the way a real retry would.
        assert _cli("disable", "smoke", dsn=dsn).returncode == 0
        status, body = _get(f"{base}/drive", login="smoke", password="other")
        assert status == 503
        assert "precis users add" in body
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            server.kill()
