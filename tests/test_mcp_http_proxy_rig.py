"""Rig for the always-up proxy in front of the shared MCP server (organizer-mcp-1).

Drives ``deploy/mcp-http/precis-mcp-http-ensure.sh`` against a *fake* backend
(a stdlib HTTP server in a stock python image) on its own container NAME, host
PORT, STATE dir, HOME and docker network, and checks what the design promises:

* a client hammering ``POST /mcp`` through the proxy every 100 ms never sees a
  refused/reset connection or a non-200 during a full blue-green recreate;
* an SSE ``GET /mcp`` opened on the old backend is not cut by ``caddy reload``;
  it ends when the old backend is stopped (the drain);
* a bad Caddyfile or a backend that never becomes ready leaves the old backend
  live, and the script exits non-zero saying so;
* the switch is logged; the one-time migration from the single-container layout
  and the proxy's own recreate are measured, not asserted away.

Skipped unless docker is reachable AND ``PRECIS_MCP_PROXY_RIG=1`` (so CI skips
it). Needs the pinned Caddy image and ``python:3.12-slim-bookworm`` locally;
never touches the live ``precis-mcp-http`` containers, network or port.
Run serially for honest timings::

    PRECIS_MCP_PROXY_RIG=1 uv run pytest tests/test_mcp_http_proxy_rig.py -n0 -s
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh"
_FAKE_IMAGE = os.environ.get("PRECIS_MCP_RIG_IMAGE", "python:3.12-slim-bookworm")
_TOKEN = "rig-token-0123456789abcdef"


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=20,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX-only: runs the bash ensure script against docker",
    ),
    pytest.mark.skipif(
        os.environ.get("PRECIS_MCP_PROXY_RIG") != "1",
        reason="set PRECIS_MCP_PROXY_RIG=1 to run the docker proxy rig",
    ),
    pytest.mark.skipif(
        os.environ.get("PRECIS_MCP_PROXY_RIG") == "1" and not _docker_ok(),
        reason="docker is not available",
    ),
]

# The fake backend. POST /mcp: 401 without Authorization, else 200 JSON naming
# its colour. GET /mcp (Authorization): an SSE event every 0.5 s for ?secs=N,
# tagged with the colour; on SIGTERM it ends its streams at the next tick (the
# drain, after control.json's drain_secs) and exits. /src/control.json (read at
# start) can also delay start-up or make the backend never listen.
_FAKE_BACKEND = r"""
import json, os, signal, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

COLOUR = os.environ.get("PRECIS_MCP_COLOUR", "?")
try:
    ctl = json.load(open("/src/control.json", encoding="utf-8"))
except OSError:
    ctl = {}
draining = threading.Event()


def _drain():
    # A slow drain, like a real server finishing in-flight calls: the port stays
    # open (new requests still answered) and open streams keep flowing.
    time.sleep(float(ctl.get("drain_secs", 0)))
    draining.set()
    if srv is not None:
        srv.shutdown()
    else:
        os._exit(0)


def _bye(*_a):
    threading.Thread(target=_drain, daemon=True).start()


srv = None
signal.signal(signal.SIGTERM, _bye)
time.sleep(float(ctl.get("startup_delay", 0)))
if ctl.get("never_ready"):
    while True:
        time.sleep(1)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Backend", COLOUR)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if not self.headers.get("Authorization"):
            return self._send(401, {"error": "unauthorized"})
        self._send(200, {"colour": COLOUR})

    def do_GET(self):
        if not self.headers.get("Authorization"):
            return self._send(401, {"error": "unauthorized"})
        secs = float(parse_qs(urlparse(self.path).query).get("secs", ["30"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-Backend", COLOUR)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        t0 = time.time()
        i = 0
        try:
            while time.time() - t0 < secs and not draining.is_set():
                self.wfile.write(f"data: {json.dumps({'colour': COLOUR, 'i': i})}\n\n".encode())
                self.wfile.flush()
                i += 1
                time.sleep(0.5)
            self.wfile.write(b"event: end\ndata: {}\n\n")
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True


srv = ThreadingHTTPServer(("0.0.0.0", 8765), H)
srv.serve_forever()
srv.server_close()
"""


def _free_port() -> int:
    for port in range(18800, 19000):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 18800-18999")


@dataclass
class Rig:
    name: str
    port: int
    root: Path
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def log_path(self) -> Path:
        return self.state / "ensure.log"

    def env(self, **over: str) -> dict[str, str]:
        real_home = Path.home()
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            # The docker CLI finds its context (colima) under the REAL home.
            "DOCKER_CONFIG": os.environ.get(
                "DOCKER_CONFIG", str(real_home / ".docker")
            ),
            "PRECIS_MCP_HTTP_NAME": self.name,
            "PRECIS_MCP_HTTP_PORT": str(self.port),
            "PRECIS_MCP_HTTP_STATE": str(self.state),
            "PRECIS_MCP_REPO": str(self.repo),
            "PRECIS_MCP_IMAGE": _FAKE_IMAGE,
            "PRECIS_MCP_RUN_CMD": "exec python /src/fake_backend.py",
            "PRECIS_MCP_ENTRYPOINT": "/usr/bin/env",
            "PRECIS_MCP_DB_HOST": "dbnode",
            "PRECIS_MCP_DB_IP": "203.0.113.10",
            # An explicit subnet: a busy dev machine has used up docker's default
            # address pools (30 compose test networks), and then network create fails.
            "PRECIS_MCP_NET_SUBNET": f"10.213.{self.port - 18800}.0/24",
            "PRECIS_MCP_HTTP_STOP_TIMEOUT": "15",
            "PRECIS_MCP_HTTP_READY_TIMEOUT": "60",
        }
        if "DOCKER_HOST" in os.environ:
            env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
        env.update(self.extra_env)
        env.update(over)
        return env

    def run(self, *args: str, **over: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(_SCRIPT), *args],
            env=self.env(**over),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )

    def docker(self, *args: str) -> str:
        r = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        return r.stdout.strip()

    def ps(self) -> list[str]:
        out = self.docker(
            "ps", "-a", "--filter", f"name=^/{self.name}", "--format", "{{.Names}}"
        )
        return sorted(out.split())

    def container_id(self, name: str) -> str:
        return self.docker("inspect", "-f", "{{.Id}}", name)

    def log_lines(self) -> list[str]:
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def post(self, auth: bool = True, timeout: float = 5.0) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            headers = {"Content-Type": "application/json"}
            if auth:
                headers["Authorization"] = f"Bearer {_TOKEN}"
            conn.request("POST", "/mcp", body="{}", headers=headers)
            r = conn.getresponse()
            r.read()
            return r.status, r.getheader("X-Backend") or ""
        finally:
            conn.close()

    def caddyfile(self) -> str:
        return (self.state / "caddy" / "Caddyfile").read_text(encoding="utf-8")

    def cleanup(self) -> None:
        ids = self.docker("ps", "-aq", "--filter", f"name=^/{self.name}").split()
        if ids:
            subprocess.run(
                ["docker", "rm", "-f", *ids], capture_output=True, check=False
            )
        subprocess.run(
            ["docker", "network", "rm", f"{self.name}-net"],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["docker", "volume", "rm", f"{self.name}-uv-cache"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def rig() -> Iterator[Rig]:
    # Under the real home: colima bind-mounts $HOME, not /var/folders.
    base = Path.home() / ".cache" / "precis-mcp-rig"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="rig-", dir=base))
    r = Rig(name=f"precis-mcp-rig-{uuid.uuid4().hex[:8]}", port=_free_port(), root=root)
    secrets = root / "home" / ".secrets" / "pw"
    secrets.mkdir(parents=True)
    (secrets / "PRECIS_MCP_TOKEN").write_text(_TOKEN + "\n", encoding="utf-8")
    (secrets / "PRECIS_DATABASE_URL").write_text(
        ("postgresql://u" + ":p@host.docker.internal:6432/db\n"), encoding="utf-8"
    )
    (root / "home" / "work" / "corpus").mkdir(parents=True)
    (root / "tmp").mkdir()
    r.repo.mkdir()
    (r.repo / "fake_backend.py").write_text(_FAKE_BACKEND, encoding="utf-8")
    try:
        yield r
    finally:
        r.cleanup()


# --- client-side probes --------------------------------------------------------


class Hammer:
    """POST /mcp (with token) every 100 ms through the proxy; keep every outcome."""

    def __init__(self, rig: Rig, interval: float = 0.1, timeout: float = 30.0) -> None:
        self.rig, self.interval, self.timeout = rig, interval, timeout
        self.results: list[
            tuple[float, str, str, float]
        ] = []  # (t, outcome, colour, seconds)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                status, colour = self.rig.post(timeout=self.timeout)
                outcome = str(status)
            except (OSError, http.client.HTTPException) as e:
                outcome, colour = f"ERR {type(e).__name__}: {e}", ""
            dt = time.time() - t0
            self.results.append((t0, outcome, colour, dt))
            self._stop.wait(max(0.0, self.interval - dt))

    def __enter__(self) -> Hammer:
        self._t.start()
        return self

    def __exit__(self, *_a: object) -> None:
        self._stop.set()
        self._t.join(timeout=60)

    def bad(self) -> list[tuple[float, str, str, float]]:
        return [r for r in self.results if r[1] != "200"]

    def colours(self) -> list[str]:
        return [r[2] for r in self.results if r[1] == "200"]

    def first(self, colour: str) -> float | None:
        for t, outcome, c, _ in self.results:
            if outcome == "200" and c == colour:
                return t
        return None

    def worst(self) -> float:
        return max((r[3] for r in self.results), default=0.0)


class Sse:
    """One SSE GET /mcp through the proxy; records (t, colour) per event and the end."""

    def __init__(self, rig: Rig, secs: int = 120) -> None:
        self.events: list[tuple[float, str]] = []
        self.ended_at: float | None = None
        self.end_reason = ""
        self.opened = threading.Event()
        self._rig, self._secs = rig, secs
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        assert self.opened.wait(30), "SSE stream did not open"

    def _loop(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self._rig.port, timeout=120)
        try:
            conn.request(
                "GET",
                f"/mcp?secs={self._secs}",
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
            r = conn.getresponse()
            assert r.getheader("Content-Type") == "text/event-stream"
            self.opened.set()
            while True:
                line = r.readline()
                if not line:
                    self.end_reason = "eof"
                    break
                text = line.decode().strip()
                if text.startswith("data:") and "colour" in text:
                    self.events.append((time.time(), json.loads(text[5:])["colour"]))
                elif text == "event: end":
                    self.end_reason = "end-event"
        except (OSError, http.client.HTTPException) as e:
            self.end_reason = f"ERR {type(e).__name__}"
            self.opened.set()
        finally:
            self.ended_at = time.time()
            conn.close()

    def wait_end(self, timeout: float) -> None:
        self._t.join(timeout)


class DieEvents:
    """`docker events` for container die, with nanosecond stamps by name."""

    def __init__(self, rig: Rig) -> None:
        self.died: dict[str, float] = {}
        self._p = subprocess.Popen(
            [
                "docker",
                "events",
                "--filter",
                "type=container",
                "--filter",
                "event=die",
                "--format",
                "{{json .}}",
            ],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._rig = rig
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        time.sleep(0.5)

    def _loop(self) -> None:
        assert self._p.stdout is not None
        for line in self._p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            name = ev.get("Actor", {}).get("Attributes", {}).get("name", "")
            if name.startswith(self._rig.name):
                self.died[name] = ev["timeNano"] / 1e9

    def close(self) -> None:
        self._p.terminate()
        self._p.wait(timeout=10)


def _install(rig: Rig) -> None:
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.ps() == sorted([f"{rig.name}-a", f"{rig.name}-proxy"])


# --- tests ----------------------------------------------------------------------


def test_install_status_and_noop(rig: Rig) -> None:
    t0 = time.time()
    _install(rig)
    print(f"\n[rig] fresh install (network, backend a, proxy): {time.time() - t0:.1f}s")
    assert rig.post(auth=False)[0] == 401
    assert rig.post() == (200, "a")
    assert (rig.state / "backend").read_text(encoding="utf-8").strip() == "a"
    assert "proxy: none→a" in "\n".join(rig.log_lines())
    # Backends publish no host port; only the proxy does.
    assert rig.docker("port", f"{rig.name}-a") == ""
    assert f"127.0.0.1:{rig.port}" in rig.docker("port", f"{rig.name}-proxy")

    st = rig.run("--status")
    assert st.returncode == 0
    assert "/proxy-health -> 200" in st.stdout
    assert "(no token, from the proxy) -> 401" in st.stdout
    assert "(no token, via proxy) -> 401" in st.stdout

    ids = {n: rig.container_id(n) for n in rig.ps()}
    t0 = time.time()
    again = rig.run()
    print(
        f"[rig] no-op ensure on a healthy install: rc={again.returncode} {time.time() - t0:.1f}s"
    )
    assert again.returncode == 0
    assert {n: rig.container_id(n) for n in rig.ps()} == ids

    cfg = rig.run("--config")
    assert f"http://127.0.0.1:{rig.port}/mcp" in cfg.stdout


def test_blue_green_swap_keeps_port_up_and_drains_sse(rig: Rig) -> None:
    # The old backend drains for 5 s after SIGTERM, streams still flowing.
    (rig.repo / "control.json").write_text('{"drain_secs": 5}', encoding="utf-8")
    _install(rig)
    a_id = rig.container_id(f"{rig.name}-a")
    deaths = DieEvents(rig)
    try:
        with Hammer(rig) as hammer:
            sse = Sse(rig)
            time.sleep(2.0)
            t_start = time.time()
            r = rig.run("--recreate")
            t_end = time.time()
            time.sleep(2.0)
            n_before_end = len(sse.events)
            sse.wait_end(30)
        time.sleep(0.5)
    finally:
        deaths.close()
    assert r.returncode == 0, r.stderr

    total = len(hammer.results)
    bad = hammer.bad()
    first_b = hammer.first("b")
    assert first_b is not None, "no request ever reached the new backend"
    died_a = deaths.died[f"{rig.name}-a"]
    last_sse = sse.events[-1][0]
    after_switch = [t for t, c in sse.events if t > first_b + 0.3]
    print(
        f"\n[rig] recreate a->b took {t_end - t_start:.1f}s; "
        f"hammer {total} POSTs, {len(bad)} not-200, slowest {hammer.worst() * 1000:.0f} ms; "
        f"first b at +{first_b - t_start:.1f}s"
    )
    print(
        f"[rig] SSE on old backend: {len(sse.events)} events, {len(after_switch)} after the switch; "
        f"stream ended {sse.end_reason!r} at {(sse.ended_at or 0.0) - died_a:+.2f}s vs old backend die "
        f"(last event {died_a - last_sse:+.2f}s before die); script returned {t_end - died_a:+.2f}s vs die"
    )

    assert not bad, f"requests failed during the swap: {bad[:5]}"
    cs = hammer.colours()
    assert cs[0] == "a" and cs[-1] == "b"
    assert "ba" not in "".join(cs), "traffic flipped back to the old colour"
    # The reload did not cut the stream: events kept coming on the old backend
    # after traffic had moved, until the old backend was stopped.
    assert len(after_switch) >= 6, "SSE stream was cut at the reload"
    assert all(c == "a" for _, c in sse.events), "SSE events from the wrong backend"
    assert sse.ended_at is not None and abs(sse.ended_at - died_a) < 3.0
    assert n_before_end >= len(after_switch)
    # State: only b + proxy remain; state file and Caddyfile agree.
    assert rig.ps() == sorted([f"{rig.name}-b", f"{rig.name}-proxy"])
    assert (rig.state / "backend").read_text(encoding="utf-8").strip() == "b"
    assert f"{rig.name}-b:8765" in rig.caddyfile()
    assert rig.container_id(f"{rig.name}-b") != a_id
    # The switch line, in the documented shape.
    assert any(
        re.fullmatch(r"proxy: a→b \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", ln)
        for ln in rig.log_lines()
    ), rig.log_lines()
    assert any(ln.startswith("drain: ") for ln in rig.log_lines())
    assert rig.post() == (200, "b")


def test_hash_change_swaps_and_second_run_is_a_noop(rig: Rig) -> None:
    _install(rig)
    r = rig.run(PRECIS_MCP_TOOL_CONCURRENCY="7")  # a spec change, no flag
    assert r.returncode == 0, r.stderr
    assert rig.post() == (200, "b")
    ids = {n: rig.container_id(n) for n in rig.ps()}
    again = rig.run(PRECIS_MCP_TOOL_CONCURRENCY="7")
    assert again.returncode == 0
    assert {n: rig.container_id(n) for n in rig.ps()} == ids


def test_bad_caddyfile_leaves_old_config_live(rig: Rig) -> None:
    _install(rig)
    before = rig.caddyfile()
    with Hammer(rig) as hammer:
        time.sleep(1.0)
        r = rig.run("--recreate", PRECIS_MCP_CADDY_EXTRA="bogus_directive_xyz")
        time.sleep(1.0)
    assert r.returncode != 0
    assert "REJECTED" in r.stderr and "stay live" in r.stderr, r.stderr
    assert rig.caddyfile() == before
    assert list((rig.state / "caddy").glob("Caddyfile.new.*")) == []
    assert (rig.state / "backend").read_text(encoding="utf-8").strip() == "a"
    assert rig.ps() == sorted([f"{rig.name}-a", f"{rig.name}-proxy"]), (
        "new backend not cleaned up"
    )
    assert not hammer.bad(), hammer.bad()[:5]
    assert set(hammer.colours()) == {"a"}
    assert not any("→" in ln for ln in rig.log_lines() if ln.startswith("proxy: a"))
    print(
        f"\n[rig] bad Caddyfile: rc={r.returncode}, {len(hammer.results)} POSTs all 200 on a"
    )


def test_failed_reload_restores_old_caddyfile(rig: Rig) -> None:
    # Passes `caddy validate` (it opens no listeners) but cannot bind at reload.
    _install(rig)
    before = rig.caddyfile()
    with Hammer(rig) as hammer:
        time.sleep(1.0)
        r = rig.run("--recreate", PRECIS_MCP_CADDY_EXTRA="bind 203.0.113.9")
        time.sleep(1.0)
    assert r.returncode != 0
    assert "reload FAILED" in r.stderr and "stay live" in r.stderr, r.stderr
    assert rig.caddyfile() == before
    assert (rig.state / "backend").read_text(encoding="utf-8").strip() == "a"
    assert rig.ps() == sorted([f"{rig.name}-a", f"{rig.name}-proxy"])
    assert not hammer.bad(), hammer.bad()[:5]
    assert set(hammer.colours()) == {"a"}
    print(
        f"\n[rig] failed reload: rc={r.returncode}, {len(hammer.results)} POSTs all 200 on a"
    )


def test_backend_never_ready_keeps_old_live(rig: Rig) -> None:
    _install(rig)
    (rig.repo / "control.json").write_text('{"never_ready": true}', encoding="utf-8")
    t0 = time.time()
    with Hammer(rig) as hammer:
        time.sleep(0.5)
        r = rig.run("--recreate", PRECIS_MCP_HTTP_READY_TIMEOUT="6")
        elapsed = time.time() - t0
    assert r.returncode != 0
    assert "did not answer 401" in r.stderr and "stays live" in r.stderr, r.stderr
    assert 6 <= elapsed < 40
    assert rig.ps() == sorted([f"{rig.name}-a", f"{rig.name}-proxy"])
    assert (rig.state / "backend").read_text(encoding="utf-8").strip() == "a"
    assert not hammer.bad() and set(hammer.colours()) == {"a"}
    # And the next attempt, with the fault gone, goes through.
    (rig.repo / "control.json").unlink()
    ok = rig.run("--recreate")
    assert ok.returncode == 0, ok.stderr
    assert rig.post() == (200, "b")
    print(
        f"\n[rig] never-ready: rc={r.returncode} after {elapsed:.1f}s (timeout 6s), old backend kept serving"
    )


def test_live_name_refuses_overrides(rig: Rig) -> None:
    # --config touches no docker; the refusal is checked before it. Safe even if
    # this regressed: it would only print a config from the rig's own HOME.
    r = rig.run("--config", PRECIS_MCP_HTTP_NAME="precis-mcp-http")
    assert r.returncode == 3
    assert "refusing to touch the live precis-mcp-http" in r.stderr
    for var in (
        "PRECIS_MCP_RUN_CMD",
        "PRECIS_MCP_CADDY_EXTRA",
        "PRECIS_MCP_CADDY_IMAGE",
        "PRECIS_MCP_ENTRYPOINT",
    ):
        env = {
            k: v
            for k, v in rig.env(PRECIS_MCP_HTTP_NAME="precis-mcp-http").items()
            if k
            not in (
                "PRECIS_MCP_HTTP_PORT",
                "PRECIS_MCP_HTTP_STATE",
                "PRECIS_MCP_IMAGE",
                "PRECIS_MCP_RUN_CMD",
                "PRECIS_MCP_ENTRYPOINT",
            )
        }
        env[var] = "x"
        p = subprocess.run(
            ["bash", str(_SCRIPT), "--config"],
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )  # fmt: skip
        assert p.returncode == 3 and var in p.stderr, (var, p.stderr)


def test_migration_from_single_container_layout(rig: Rig) -> None:
    legacy_cmd = [
        "docker", "run", "-d", "--name", rig.name,
        "--label", "precis.env_hash=legacy", "--restart", "unless-stopped",
        "-p", f"127.0.0.1:{rig.port}:8765",
        "-v", f"{rig.repo}:/src:ro",
        "-e", "PRECIS_MCP_COLOUR=legacy",
        "--entrypoint", "/usr/bin/env", _FAKE_IMAGE,
        "bash", "-c", "exec python /src/fake_backend.py",
    ]  # fmt: skip
    p = subprocess.run(
        legacy_cmd, capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert p.returncode == 0, p.stderr
    for _ in range(100):
        try:
            if rig.post(auth=False, timeout=2)[0] == 401:
                break
        except OSError:
            time.sleep(0.2)
    legacy_id = rig.container_id(rig.name)

    # An ordinary run leaves the legacy container serving.
    r = rig.run()
    assert r.returncode == 0 and "--migrate" in r.stderr
    assert rig.container_id(rig.name) == legacy_id

    # Probe fast during the cutover: the refused window is the one known gap.
    probes: list[tuple[float, str]] = []
    stop = threading.Event()

    def probe() -> None:
        while not stop.is_set():
            t = time.time()
            try:
                s, c = rig.post(timeout=3)
                probes.append((t, f"{s}:{c}"))
            except (OSError, http.client.HTTPException) as e:
                probes.append((t, f"ERR {type(e).__name__}"))
            time.sleep(0.02)

    th = threading.Thread(target=probe, daemon=True)
    th.start()
    time.sleep(0.5)
    t0 = time.time()
    m = rig.run("--migrate")
    t1 = time.time()
    time.sleep(1.0)
    stop.set()
    th.join(30)
    assert m.returncode == 0, m.stderr

    bad = [t for t, o in probes if not o.startswith("200:")]
    gap = (max(bad) - min(bad)) if bad else 0.0
    print(
        f"\n[rig] migration took {t1 - t0:.1f}s; {len(probes)} probes, {len(bad)} failed; "
        f"dark window {gap:.2f}s (first fail to last fail)"
    )
    assert probes[-1][1] == "200:a"
    assert gap < 15.0
    assert rig.ps() == sorted(
        [f"{rig.name}-a", f"{rig.name}-proxy"]
    )  # legacy container gone
    assert "proxy: legacy→a" in "\n".join(rig.log_lines())
    assert rig.post(auth=False)[0] == 401


def test_recreate_proxy_gap_is_short_and_state_survives(rig: Rig) -> None:
    _install(rig)
    old_proxy = rig.container_id(f"{rig.name}-proxy")
    probes: list[tuple[float, str]] = []
    stop = threading.Event()

    def probe() -> None:
        while not stop.is_set():
            t = time.time()
            try:
                s, c = rig.post(timeout=3)
                probes.append((t, f"{s}:{c}"))
            except (OSError, http.client.HTTPException) as e:
                probes.append((t, f"ERR {type(e).__name__}"))
            time.sleep(0.02)

    th = threading.Thread(target=probe, daemon=True)
    th.start()
    time.sleep(0.5)
    t0 = time.time()
    r = rig.run("--recreate-proxy")
    t1 = time.time()
    time.sleep(1.0)
    stop.set()
    th.join(30)
    assert r.returncode == 0, r.stderr
    bad = [t for t, o in probes if not o.startswith("200:")]
    gap = (max(bad) - min(bad)) if bad else 0.0
    print(
        f"\n[rig] --recreate-proxy took {t1 - t0:.1f}s; {len(bad)} of {len(probes)} probes failed; dark window {gap:.2f}s"
    )
    assert gap < 15.0
    assert rig.container_id(f"{rig.name}-proxy") != old_proxy
    assert probes[-1][1] == "200:a"
    assert rig.ps() == sorted([f"{rig.name}-a", f"{rig.name}-proxy"])


def test_request_waits_for_a_backend_that_is_coming_up(rig: Rig) -> None:
    # lb_try_duration: a dial failure (backend stopped, its name not resolvable)
    # is retried for up to 60 s, so the request waits instead of being refused.
    _install(rig)
    assert rig.post() == (200, "a")  # warm: leaves an upstream connection to reuse
    rig.docker("stop", "-t", "2", f"{rig.name}-a")
    result: list[tuple[float, tuple[int, str] | str]] = []

    def call() -> None:
        t0 = time.time()
        try:
            answer = rig.post(timeout=40)
            result.append((time.time() - t0, answer))
        except (OSError, http.client.HTTPException) as e:
            result.append((time.time() - t0, f"ERR {type(e).__name__}"))

    th = threading.Thread(target=call)
    th.start()
    time.sleep(3.0)
    rig.docker("start", f"{rig.name}-a")
    th.join(60)
    waited, outcome = result[0]
    print(
        f"\n[rig] POST issued with the backend stopped: answered {outcome} after {waited:.1f}s"
    )
    assert outcome == (200, "a")
    assert 2.5 < waited < 20
