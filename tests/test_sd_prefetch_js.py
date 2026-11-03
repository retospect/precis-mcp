"""Client-JS coverage for the smartdraft reader's prefetch/render cache
(``static/sd-prefetch.js``, gr553319): the ±3 prefetch window, the LRU
entries, abort-on-jump, drop-on-edit and the post-mutation epoch guard.

``scripts/sd_prefetch_smoke.mjs`` does the asserting; this wrapper runs it
and skips when ``node`` is absent (as ``test_se_view_state_js.py``). The
source pins below hold the wiring without a browser: ``#sd-state`` carries
the ``v`` render token, the module is loaded as ESM, and the nav IIFE calls
into it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests._node import needs_node

_REPO = Path(__file__).resolve().parents[1]
_VIEW = (
    _REPO / "src" / "precis_web" / "templates" / "smartdraft" / "view.html.j2"
).read_text(encoding="utf-8")


@needs_node
@pytest.mark.skipif(sys.platform == "win32", reason="node ESM path quirk on Windows")
def test_sd_prefetch_smoke() -> None:
    result = subprocess.run(
        ["node", str(_REPO / "scripts" / "sd_prefetch_smoke.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_sd_state_carries_the_render_token() -> None:
    # The cache entry's staleness key — `render_version` in the route.
    assert '"v": view_version' in _VIEW
    assert '"focusHandle"' in _VIEW  # drop-on-edit matches entries by it


def test_the_module_is_loaded_and_wired() -> None:
    assert "import * as _sdPrefetch from '/static/sd-prefetch.js'" in _VIEW
    # The cache path: serve cached → background revalidate → refill window.
    assert "rc.get(url)" in _VIEW
    assert "revalidate(url)" in _VIEW
    assert "prefetchAround()" in _VIEW
    # A mutation refresh drops everything and bypasses the cache.
    assert "rc.clear()" in _VIEW
    assert "{ force: true }" in _VIEW
    # The draft:* edit events drop the edited block's own entry.
    assert "__sdInvalidate" in _VIEW


def test_review_fixes_are_wired() -> None:
    # Real navigations pin their fetch — reap()/clear() must never abort
    # a request a nav is awaiting (that was the jump→location.href bug).
    assert "{ pinned: true }" in _VIEW
    # Every location.href fallback is gated on the URL still being current.
    assert (
        "const fallback = () => { if (isCurrent(url)) location.href = url; };" in _VIEW
    )
    # Prefetch + background revalidate are debounced ~150ms after settle.
    assert "scheduleSettle" in _VIEW
    # A revalidate never swaps over an open inline editor — it marks the
    # entry stale instead so the next nav refetches.
    assert "panelBusy()" in _VIEW
    assert "markStale" in _VIEW
