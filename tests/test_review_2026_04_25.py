"""Regression tests for the 2026-04-25 architecture review.

Three fixes covered:

1. **MCP ``put(unlink=...)``** — the MCP tool surface in
   :mod:`precis.server` was missing the ``unlink`` parameter that
   ``tools.put`` already supported, so agents could create links via
   MCP but never remove them.  See review CRITICAL-MAJOR.

2. **Dispatch handler-cache fix** — :func:`server._dispatch` used to
   construct a fresh ``registered.handler_cls()`` for every call,
   bypassing :data:`registry._SCHEME_INSTANCES` and forcing every kind
   that holds warm state (HTTP clients, DB pools, parsed indexes) to
   re-init on every request.  Now :func:`registry.resolve` is used so
   the same cached instance is returned.

3. **Canonical separator flipped to ASCII ``~``** — the previous
   canonical was U+203A (``›``).  Small models confuse it with ``>``,
   ``'``, U+2039, etc.  Output is now always ``~``; legacy ``›`` is
   still accepted on input for back-compat.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from precis import server, tools
from precis.registry import (
    KINDS,
    _discover,
    _reset_instance_cache,
    resolve,
)
from precis.uri import SEP, parse

# ---------------------------------------------------------------------------
# Fix 1 — MCP put(unlink=...) is reachable
# ---------------------------------------------------------------------------


class TestUnlinkOnMCPSurface:
    """The MCP ``put`` tool must expose every primitive ``tools.put``
    declares; otherwise the advertisement at ``tools/list`` is a lie
    (mcp-critic rule B1)."""

    def test_unlink_param_in_signature(self):
        sig = inspect.signature(server.put)
        assert (
            "unlink" in sig.parameters
        ), "server.put() is missing 'unlink=' — agents cannot remove links via MCP"

    def test_unlink_default_is_empty_string(self):
        # Default must be falsy so the parameter is optional and the
        # legacy zero-arg call shape (``put(id=..., text=..., mode=...)``)
        # keeps working unchanged.
        sig = inspect.signature(server.put)
        assert sig.parameters["unlink"].default == ""

    def test_unlink_dispatches_through_tools_put(self):
        # End-to-end: server.put(unlink=...) reaches tools.put(unlink=...).
        # Patch tools.put to capture the kwargs without hitting the store.
        with patch.object(server.tools, "put", return_value="ok") as mock_put:
            server.put(id="memory:x", unlink="memory:y:cites")
        assert mock_put.called
        kwargs = mock_put.call_args.kwargs
        assert kwargs["unlink"] == "memory:y:cites"

    def test_unlink_present_in_tool_docstring(self):
        # Description must explain the parameter — the MCP tool listing
        # is the agent's only documentation surface (rule B1 / B2).
        assert "unlink" in (server.put.__doc__ or "").lower()


# ---------------------------------------------------------------------------
# Fix 2 — _dispatch reuses the cached handler instance
# ---------------------------------------------------------------------------


class TestDispatchUsesCachedHandler:
    """Two calls to the same kind must reach the same handler instance.

    The previous code path constructed ``registered.handler_cls()`` on
    every dispatch, splitting the actual call (cached) from the
    ``hints()`` / ``cost_of()`` hooks (fresh).  With the v5.3 fix,
    ``_dispatch`` resolves through :func:`registry.resolve`, which
    returns the same memoised instance ``tools.read`` / ``tools.put``
    use.
    """

    def setup_method(self) -> None:
        _discover()
        _reset_instance_cache()

    def test_same_handler_instance_across_calls(self):
        # Use ``calc`` because it has no env requirements and no DB —
        # the cached handler is the simplest case.
        if "calc" not in KINDS:
            pytest.skip("calc kind not registered (no sympy installed)")
        h1 = resolve("calc", "")
        h2 = resolve("calc", "")
        assert h1 is h2

    def test_dispatch_resolves_cached_instance(self):
        # Force a cache miss → hit pattern by clearing then dispatching
        # through invoke_handler.  Capture the handler that gets passed
        # to ``invoke_handler`` and assert it equals the cached one.
        if "calc" not in KINDS:
            pytest.skip("calc kind not registered (no sympy installed)")

        seen: list[object] = []

        def _capture(kind, verb, handler, fn, *, args=None):
            seen.append(handler)
            from precis.protocol import Result

            return Result.ok("ok", kind=kind, cost="free")

        with patch.object(server, "invoke_handler", side_effect=_capture):
            server._dispatch("calc", "get", lambda: "ok", args={"id": "1+1"})
            server._dispatch("calc", "get", lambda: "ok", args={"id": "2+2"})

        # Both dispatches saw the same handler instance.
        assert len(seen) == 2
        assert seen[0] is seen[1]
        # And it's the same instance ``resolve`` returns.
        assert seen[0] is resolve("calc", "")


# ---------------------------------------------------------------------------
# Fix 3 — canonical SEP is ``~`` and ``›`` is still accepted
# ---------------------------------------------------------------------------


class TestSeparatorFlip:
    """Canonical separator must be ASCII ``~`` for small-model
    compatibility (mcp-critic rule E3 — CRITICAL).  The legacy U+203A
    must keep parsing on input, but every output / docstring / example
    uses ``~``."""

    def test_canonical_sep_is_ascii_tilde(self):
        assert SEP == "~"
        # Belt-and-braces: ASCII codepoint, not the Unicode lookalike.
        assert ord(SEP) == 0x7E

    def test_legacy_sep_still_parses_index(self):
        p = parse("paper:foo\u203a38")
        assert p.selector == "38"
        assert p.range_start == 38

    def test_legacy_sep_still_parses_slug(self):
        p = parse("file:planning.docx\u203aKR8M2")
        assert p.selector == "KR8M2"
        assert p.selector_type == "slug"

    def test_legacy_sep_still_parses_range(self):
        p = parse("paper:foo\u203a38..42")
        assert p.range_start == 38
        assert p.range_end == 42

    def test_canonical_and_legacy_parse_to_same_result(self):
        canonical = parse("paper:foo~38..42")
        legacy = parse("paper:foo\u203a38..42")
        assert canonical.selector == legacy.selector
        assert canonical.range_start == legacy.range_start
        assert canonical.range_end == legacy.range_end
        assert canonical.scheme == legacy.scheme
        assert canonical.path == legacy.path

    def test_legacy_sep_absent_from_get_docstring(self):
        # The agent reads ``server.get.__doc__`` via ``tools/list``;
        # exposing the legacy U+203A there teaches the agent to emit
        # the lookalike character.  Every example must use ``~``.
        doc = server.get.__doc__ or ""
        assert "\u203a" not in doc, (
            "server.get docstring still mentions U+203A (\u203a); "
            "examples must use ASCII ~ — see mcp-critic rule E3"
        )

    def test_legacy_sep_absent_from_put_docstring(self):
        doc = server.put.__doc__ or ""
        assert "\u203a" not in doc

    def test_legacy_sep_absent_from_move_docstring(self):
        doc = server.move.__doc__ or ""
        assert "\u203a" not in doc

    def test_tools_read_docstring_uses_canonical_sep(self):
        doc = tools.read.__doc__ or ""
        assert "\u203a" not in doc
        # And the canonical form is mentioned, not just absent.
        assert "~selector" in doc or "[~selector]" in doc

    def test_tools_put_docstring_uses_canonical_sep(self):
        doc = tools.put.__doc__ or ""
        assert "\u203a" not in doc
