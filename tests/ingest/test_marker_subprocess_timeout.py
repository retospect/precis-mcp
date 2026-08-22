"""Tests for the P2-3 hang guard on the live Marker extraction path.

``marker`` (and torch) aren't importable on the host, so these tests
never touch ``_marker_extract`` / ``_marker_extract_subprocess``
directly. Instead they exercise the generic spawn-child-with-timeout
mechanism (:func:`precis.ingest.marker._run_in_subprocess_with_timeout`)
with module-level target functions from
:mod:`tests.ingest._marker_subprocess_targets` — ``multiprocessing``'s
``spawn`` context pickles the target by reference, so a closure or a
monkeypatched function defined inside a test can never cross the
process boundary; only a real, importable module-level function can.
"""

from __future__ import annotations

import multiprocessing
import time

import pytest

from precis.ingest.marker import (
    _marker_extract_subprocess,
    _run_in_subprocess_with_timeout,
    extract_blocks_marker,
)
from tests.ingest import _marker_subprocess_targets as targets

# Keep the sleeper test fast: the guard timeout, not the sleep duration,
# is what's under test.
_SLEEPER_TIMEOUT_S = 1.0


class TestRunInSubprocessWithTimeout:
    def test_success_round_trips_result(self):
        result = _run_in_subprocess_with_timeout(
            targets.fast_return,
            args=(2, 3),
            timeout_s=10.0,
            label="fast_return test",
        )
        assert result == {"sum": 5}

    def test_timeout_kills_the_child(self):
        with pytest.raises(TimeoutError, match="timed out after"):
            _run_in_subprocess_with_timeout(
                targets.sleep_forever,
                timeout_s=_SLEEPER_TIMEOUT_S,
                label="sleeper test",
            )
        # The child must actually be gone afterward — not just
        # abandoned. multiprocessing.active_children() prunes any
        # process whose _popen.poll() is no longer None, so a lingering
        # entry here would mean terminate()/kill() didn't actually work.
        deadline = time.monotonic() + 5.0
        while multiprocessing.active_children() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not multiprocessing.active_children()

    def test_child_exception_surfaces_as_runtime_error(self):
        with pytest.raises(RuntimeError, match="boom-marker-subprocess-test"):
            _run_in_subprocess_with_timeout(
                targets.raise_value_error,
                timeout_s=10.0,
                label="raiser test",
            )


class TestExtractBlocksMarkerTimeoutFallback:
    def test_falls_back_to_fitz_on_subprocess_timeout(self, monkeypatch, tmp_path):
        pdf_path = tmp_path / "wedged.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        def _fake_marker_extract_subprocess(pdf_path, paper_id, timeout_s):
            raise TimeoutError(
                f"Marker extraction of {pdf_path.name} timed out after {timeout_s}s"
            )

        fitz_calls: list[tuple] = []

        def _fake_fitz_fallback(pdf_path, paper_id):
            fitz_calls.append((pdf_path, paper_id))
            return []

        monkeypatch.setattr(
            "precis.ingest.marker._marker_extract_subprocess",
            _fake_marker_extract_subprocess,
        )
        monkeypatch.setattr(
            "precis.ingest.marker._fitz_fallback",
            _fake_fitz_fallback,
        )
        monkeypatch.setattr(
            "precis.ingest.marker._release_marker_caches",
            lambda: None,
        )

        result = extract_blocks_marker(pdf_path, "paper123", timeout_s=5.0)

        assert result == []
        assert fitz_calls == [(pdf_path, "paper123")]

    def test_fallback_info_set_on_subprocess_timeout(self, monkeypatch, tmp_path):
        """gr236139: ``fallback_info`` records the fallback even when the
        fitz fallback itself returns zero blocks — the only signal that
        survives an empty-output fallback run."""
        pdf_path = tmp_path / "wedged.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        monkeypatch.setattr(
            "precis.ingest.marker._marker_extract_subprocess",
            lambda pdf_path, paper_id, timeout_s: (_ for _ in ()).throw(
                TimeoutError(f"Marker extraction of {pdf_path.name} timed out")
            ),
        )
        monkeypatch.setattr(
            "precis.ingest.marker._fitz_fallback",
            lambda pdf_path, paper_id: [],
        )
        monkeypatch.setattr("precis.ingest.marker._release_marker_caches", lambda: None)

        fallback_info: dict = {}
        result = extract_blocks_marker(
            pdf_path, "paper123", timeout_s=5.0, fallback_info=fallback_info
        )

        assert result == []
        assert fallback_info["used_fallback"] is True
        assert "timed out" in fallback_info["reason"]

    def test_fallback_info_set_on_marker_exception(self, monkeypatch, tmp_path):
        pdf_path = tmp_path / "corrupt.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        def _raise(pdf_path, paper_id):
            raise RuntimeError("llama-server binary not found")

        monkeypatch.setattr("precis.ingest.marker._marker_extract", _raise)
        monkeypatch.setattr(
            "precis.ingest.marker._fitz_fallback",
            lambda pdf_path, paper_id: [],
        )
        monkeypatch.setattr("precis.ingest.marker._release_marker_caches", lambda: None)

        fallback_info: dict = {}
        result = extract_blocks_marker(
            pdf_path, "paper123", fallback_info=fallback_info
        )

        assert result == []
        assert fallback_info["used_fallback"] is True
        assert "llama-server binary not found" in fallback_info["reason"]

    def test_fallback_info_untouched_on_success(self, monkeypatch, tmp_path):
        pdf_path = tmp_path / "normal.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        monkeypatch.setattr(
            "precis.ingest.marker._marker_extract",
            lambda pdf_path, paper_id: [],
        )
        monkeypatch.setattr("precis.ingest.marker._release_marker_caches", lambda: None)

        fallback_info: dict = {}
        extract_blocks_marker(pdf_path, "paper123", fallback_info=fallback_info)

        assert fallback_info == {}

    def test_timeout_s_none_stays_in_process_unguarded(self, monkeypatch, tmp_path):
        """Unset ``timeout_s`` must never touch the subprocess path —
        byte-identical to pre-P2-3 behavior."""
        pdf_path = tmp_path / "normal.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        calls: list[str] = []

        def _fake_marker_extract(pdf_path, paper_id):
            calls.append("in_process")
            return []

        def _fail_if_called(*args, **kwargs):
            raise AssertionError(
                "_marker_extract_subprocess must not be called when timeout_s is unset"
            )

        monkeypatch.setattr(
            "precis.ingest.marker._marker_extract", _fake_marker_extract
        )
        monkeypatch.setattr(
            "precis.ingest.marker._marker_extract_subprocess", _fail_if_called
        )
        monkeypatch.setattr("precis.ingest.marker._release_marker_caches", lambda: None)

        result = extract_blocks_marker(pdf_path, "paper123")

        assert result == []
        assert calls == ["in_process"]


def test_marker_extract_subprocess_signature_matches_spec():
    """Documents the exact contract P2-3 specified: ``(pdf_path, paper_id,
    timeout_s) -> list[dict]``, forwarding to the generic helper."""
    import inspect

    sig = inspect.signature(_marker_extract_subprocess)
    assert list(sig.parameters) == ["pdf_path", "paper_id", "timeout_s"]
