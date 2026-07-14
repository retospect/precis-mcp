"""The MCP tool-dispatch path must reuse the server's store-bearing runtime.

Regression for the 2026-07-14 asa incident. ``precis serve`` builds a
store-bearing runtime at boot; that build calls
``secrets.adopt_process_store`` which scrubs ``PRECIS_DATABASE_URL`` from
``os.environ``. If the tool-dispatch path (``precis.tools.core``) then
lazily builds its *own* runtime, that second build reads the scrubbed env,
gets no ``database_url``, and comes up **storeless** — every DB kind
(memory / conv / gripe) reports ``unknown kind`` even though the server's
own runtime is fully connected. ``server._init_runtime`` now shares the one
runtime with the tool path via ``core.set_runtime``.
"""

from __future__ import annotations

import precis.tools.core as core


def test_set_runtime_is_reused_without_rebuilding(monkeypatch):
    sentinel = object()

    def _boom():  # build_runtime must NOT be called once a runtime is set
        raise AssertionError("tool path rebuilt a second runtime")

    monkeypatch.setattr(core, "_runtime", None)
    monkeypatch.setattr("precis.runtime.build_runtime", _boom)
    core.set_runtime(sentinel)
    assert core._get_runtime() is sentinel


def test_init_runtime_wires_the_tool_path(monkeypatch):
    """server._init_runtime shares its built runtime with tools.core."""
    import precis.server as server

    built = object()
    monkeypatch.setattr(server, "_runtime", None)
    monkeypatch.setattr(core, "_runtime", None)
    # server.py binds build_runtime at import time, so patch it there.
    monkeypatch.setattr(server, "build_runtime", lambda: built)
    # Skip the heavy modality/instruction wiring — irrelevant to this test.
    monkeypatch.setattr(server, "_wire_modalities", lambda _rt: None)
    monkeypatch.setattr(server, "_apply_instructions", lambda _a, _b: None)

    rt = server._init_runtime()
    assert rt is built
    # The tool-dispatch path now returns the SAME object — no second build.
    assert core._get_runtime() is built
