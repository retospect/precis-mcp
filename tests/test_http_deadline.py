"""gr486823: overall-deadline cover for outbound HTTP calls.

httpx timeouts are per phase — a peer that trickles a byte inside every
read window holds the call open forever. ``tests`` here run a real local
server that answers then trickles forever, and assert the call returns at
the deadline with a clear error instead of blocking.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from precis.utils.deadline import DeadlineExceeded, call_with_deadline
from precis.utils.http import request_with_deadline


class _TrickleHandler(BaseHTTPRequestHandler):
    """200 + headers, then one byte per tick forever — the worst case for
    a per-read timeout (every read returns inside its window)."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        while True:
            try:
                self.wfile.write(b" ")
                self.wfile.flush()
            except OSError:
                return  # client gone — stop trickling
            time.sleep(0.02)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def trickle_url() -> Any:
    """Yield the base URL of a trickle-forever server; torn down after."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TrickleHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


class TestCallWithDeadline:
    def test_returns_result(self) -> None:
        assert call_with_deadline(lambda: 41 + 1, deadline_s=5.0) == 42

    def test_propagates_errors(self) -> None:
        with pytest.raises(ValueError, match="inner"):
            call_with_deadline(
                lambda: (_ for _ in ()).throw(ValueError("inner")),
                deadline_s=5.0,
            )

    def test_abandons_past_deadline_and_reports_thread(self) -> None:
        aborts: list[str] = []

        def hang() -> None:
            threading.Event().wait(30)

        t0 = time.monotonic()
        with pytest.raises(DeadlineExceeded, match="deadline") as ei:
            call_with_deadline(
                hang,
                deadline_s=0.2,
                abort=lambda: aborts.append("x"),
                label="wedged-test",
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 10, f"deadline did not bound the call ({elapsed:.1f}s)"
        assert "wedged-test" in str(ei.value)
        # The abandoned work is still running — the exception hands the
        # caller its thread so it can decline to re-invoke while it lives.
        assert ei.value.thread.is_alive()
        assert aborts == ["x"]


class TestRequestWithDeadline:
    def test_trickling_peer_times_out_as_read_timeout(self, trickle_url) -> None:
        client = httpx.Client(timeout=30.0)
        t0 = time.monotonic()
        with pytest.raises(httpx.ReadTimeout, match="deadline"):
            request_with_deadline(
                client, 0.5, lambda: client.get(trickle_url), label="GET trickle"
            )
        elapsed = time.monotonic() - t0
        # Well under the per-phase 30s timeout — the overall bound is what
        # fired, not a phase timeout.
        assert elapsed < 10, f"call was not bounded ({elapsed:.1f}s)"

    def test_fast_response_returns_normally(self, trickle_url) -> None:
        # A trivial non-trickling endpoint on the same server shape is
        # covered by call_with_deadline's happy path; here just assert a
        # real 404 comes back as a response, not an error.
        class _NotFound(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _NotFound)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            client = httpx.Client(timeout=30.0)
            resp = request_with_deadline(client, 5.0, lambda: client.get(url))
            assert resp.status_code == 404
        finally:
            server.shutdown()
            server.server_close()
            t.join(timeout=5)


class TestOrcidFetchDeadline:
    def test_fetch_record_returns_upstream_error_within_deadline(
        self, trickle_url, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``fetch_record`` (the orcid_enrich path) must surface an
        ``Upstream`` at the per-call deadline, not hang in the SSL read
        that stalled balthazar's loop (gr486823)."""
        from precis.errors import Upstream
        from precis.ingest import orcid as orcid_api

        monkeypatch.setattr(orcid_api, "_API_BASE", trickle_url)
        monkeypatch.setattr(orcid_api, "_DEADLINE_S", 0.5)
        monkeypatch.setattr(orcid_api, "_get_token", lambda: "tok")
        # The pinning transport (installed by http_client) refuses
        # loopback targets, so the test substitutes a plain client — the
        # deadline seam under test is identical.
        monkeypatch.setattr(
            orcid_api,
            "http_client",
            lambda **kwargs: httpx.Client(timeout=kwargs.get("timeout") or 30.0),
        )

        t0 = time.monotonic()
        with pytest.raises(Upstream, match="transport"):
            orcid_api.fetch_record("0000-0002-1825-0097")
        elapsed = time.monotonic() - t0
        assert elapsed < 10, f"ORCID fetch was not bounded ({elapsed:.1f}s)"
