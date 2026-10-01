"""Wedge detection for the shared session MCP (precis.mcp_liveness).

Two halves. :class:`WedgeDetector` is pure state and is pinned on the
properties the backlog item's acceptance criteria name: a wedge is acted on,
load and a respawn gap are not, and repeated wedges back off rather than
loop. :func:`probe` is pinned against real sockets — a live minimal MCP
server answers, a socket that accepts and never replies (what a SIGSTOPped
server looks like from outside) fails within the timeout, a closed port fails
fast.
"""

from __future__ import annotations

import ast
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from precis import mcp_liveness
from precis.mcp_liveness import (
    LivenessPolicy,
    WedgeDetector,
    breadcrumb_path,
    probe,
    record_wedge_kill,
    run_liveness_loop,
)

_POLICY = LivenessPolicy(
    interval_s=20.0,
    timeout_s=15.0,
    failures_to_wedge=3,
    startup_grace_s=60.0,
    kill_cooldown_s=300.0,
    kill_cooldown_cap_s=1200.0,
)


def _detector() -> WedgeDetector:
    detector = WedgeDetector(policy=_POLICY)
    detector.child_started(0.0)
    return detector


def test_three_consecutive_failures_after_grace_is_a_wedge() -> None:
    detector = _detector()
    assert not detector.observe("timeout", 100.0)
    assert not detector.observe("timeout", 120.0)
    assert detector.observe("timeout", 140.0)


def test_a_success_resets_the_count() -> None:
    detector = _detector()
    detector.observe("timeout", 100.0)
    detector.observe("timeout", 120.0)
    assert not detector.observe(None, 140.0)
    assert not detector.observe("timeout", 160.0)
    assert not detector.observe("timeout", 180.0)


def test_failures_inside_the_respawn_grace_are_not_counted() -> None:
    # A respawn takes ~13 s and connections queue meanwhile: a slow or
    # failed probe then is the gap, not a wedge.
    detector = _detector()
    for t in (5.0, 20.0, 40.0, 59.0):
        assert not detector.observe("timeout", t)
    assert detector.failures == []


def test_repeat_wedges_back_off_instead_of_looping() -> None:
    detector = _detector()
    for t in (100.0, 120.0):
        detector.observe("timeout", t)
    assert detector.observe("timeout", 140.0)  # kill 1
    detector.child_started(150.0)
    # Wedges again straight after the grace: inside the 300 s cooldown.
    for t in (220.0, 240.0, 260.0, 280.0):
        assert not detector.observe("timeout", t)
    assert detector.observe("timeout", 440.0)  # 300 s after kill 1 — kill 2
    detector.child_started(450.0)
    # Two kills within the hour: the cooldown doubles to 600 s.
    for t in (520.0, 540.0, 560.0, 1000.0):
        assert not detector.observe("timeout", t)
    assert detector.observe("timeout", 1040.0)


def test_cooldown_is_capped() -> None:
    detector = WedgeDetector(policy=_POLICY)
    detector.kills = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert detector._cooldown_s() == 1200.0


def test_breadcrumb_path_is_install_watchdogs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Duplicated rather than imported (the supervisor may not import
    # precis.*), so pin the two paths together.
    from precis.install_watchdog import _breadcrumb_path

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert breadcrumb_path() == _breadcrumb_path()
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert breadcrumb_path() == _breadcrumb_path()


def test_wedge_kill_shows_on_the_next_precis_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from precis.handlers.skill import _render_last_exit_note
    from precis.install_watchdog import _reset_breadcrumb_state_for_tests

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _reset_breadcrumb_state_for_tests()
    detector = _detector()
    for t in (100.0, 120.0, 140.0):
        detector.observe("no answer", t)
    record_wedge_kill(detector, pid=4242, now=140.0)

    note = _render_last_exit_note() or ""
    _reset_breadcrumb_state_for_tests()
    assert "killed as wedged" in note
    assert "pid 4242" in note
    assert "3 failed" in note
    assert "1 wedge kill(s) in the last hour" in note


def test_imports_are_supervisor_safe() -> None:
    # The supervisor outlives every re-copy of the precis tree, so this
    # module may import stdlib and venv packages only, all at module top.
    tree = ast.parse(Path(mcp_liveness.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(n == "precis" or n.startswith("precis.") for n in names), (
            f"line {node.lineno}: precis import {names}"
        )
        assert node in tree.body, f"line {node.lineno}: import inside a function"


def test_loop_calls_on_wedge_and_stops() -> None:
    stop = threading.Event()
    wedges: list[int] = []
    detector = WedgeDetector(
        policy=LivenessPolicy(interval_s=0.01, failures_to_wedge=2, startup_grace_s=0)
    )

    def on_wedge() -> None:
        wedges.append(1)
        stop.set()

    run_liveness_loop(
        url="unused",
        token="unused",
        on_wedge=on_wedge,
        stop=stop,
        detector=detector,
        probe_fn=lambda *_: "down",
    )
    assert wedges == [1]


# --- probe against real sockets -------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_mcp() -> Iterator[str]:
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("liveness-test")

    @app.tool()
    def ping() -> str:
        return "pong"

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app.streamable_http_app(), host="127.0.0.1", port=port, log_level="error"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


def test_probe_passes_on_a_live_mcp_server(live_mcp: str) -> None:
    assert probe(live_mcp, "token", 10.0) is None


def test_probe_fails_within_timeout_on_a_server_that_never_answers() -> None:
    # Accepts the TCP connection and then says nothing: what a SIGSTOPped
    # or event-loop-blocked server looks like from the outside.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        started = time.monotonic()
        failure = probe(f"http://127.0.0.1:{port}/mcp", "token", 2.0)
        elapsed = time.monotonic() - started
    assert failure is not None
    assert "within 2s" in failure
    assert elapsed < 6.0


def test_probe_fails_fast_on_a_closed_port() -> None:
    failure = probe(f"http://127.0.0.1:{_free_port()}/mcp", "token", 10.0)
    assert failure is not None
    assert "within" not in failure
