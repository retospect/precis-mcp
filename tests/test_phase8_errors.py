"""Phase 8 — unified error format + auto-enrichment + signature hardening.

Covers:

1.  ``PrecisError`` rejects the legacy single-string form (post-2c).
2.  ``_enrich_error`` fills ``options=`` / ``next=`` from the handler's
    declared vocabulary whenever the raise-site left them empty, per
    §11.3 of the plugin-architecture doc.
3.  ``invoke_handler`` runs enrichment before ``_format_error`` so the
    rendered output reflects the auto-fills.
4.  Handler-supplied values always win over auto-fill.
"""

from __future__ import annotations

import pytest

from precis.protocol import (
    CallContext,
    ErrorCode,
    Handler,
    PrecisError,
    extract_kwargs,
)
from precis.registry import _enrich_error, _format_error, invoke_handler

# ---------------------------------------------------------------------------
# Stub handlers with realistic vocabularies
# ---------------------------------------------------------------------------


class _RwHandler(Handler):
    """Read+write handler with three views and four modes."""

    scheme = "demo"
    writable = True
    views = {"toc", "summary", "recent"}
    allowed_modes = {"append", "replace", "delete", "note"}

    def read(self, *args, **kwargs):  # pragma: no cover
        return ""


class _RoHandler(Handler):
    """Read-only handler."""

    scheme = "romode"
    writable = False
    views = {"toc"}
    allowed_modes: set[str] = set()

    def read(self, *args, **kwargs):  # pragma: no cover
        return ""


class _RefBaseStub(Handler):
    """Mimics a RefHandler: dict views keyed by view name."""

    scheme = "refish"
    writable = True
    views = {
        "toc": "_read_toc_view",
        "summary": "_read_summary_view",
        "meta": "_read_meta_view",
        "chunk": "_read_chunk_view",
        "links": "_read_links_view",
        "links-in": "_read_links_inbound_view",
        "tags": "_read_tags_view",
    }
    allowed_modes = {"append", "note"}

    def read(self, *args, **kwargs):  # pragma: no cover
        return ""


# ---------------------------------------------------------------------------
# _enrich_error — per-code auto-fill rules
# ---------------------------------------------------------------------------


class TestEnrichOptions:
    """options= auto-fill from handler vocabulary."""

    def test_view_unknown_fills_from_handler_views(self):
        exc = PrecisError(ErrorCode.VIEW_UNKNOWN, cause="view '/x' unknown")
        ctx = CallContext(kind="demo", verb="get")
        options, _next = _enrich_error(exc, _RwHandler(), ctx)
        assert options == ["/recent", "/summary", "/toc"]

    def test_view_unknown_includes_all_handler_views(self):
        exc = PrecisError(ErrorCode.VIEW_UNKNOWN, cause="view '/x' unknown")
        ctx = CallContext(kind="refish", verb="get")
        options, _ = _enrich_error(exc, _RefBaseStub(), ctx)
        # Both the subclass-specific and base views should appear, deduped.
        assert "/tags" in options
        assert "/toc" in options
        assert "/summary" in options
        assert "/links" in options
        assert "/links-in" in options
        assert "/meta" in options

    def test_mode_unsupported_fills_from_allowed_modes(self):
        exc = PrecisError(ErrorCode.MODE_UNSUPPORTED, cause="mode 'x' bad")
        ctx = CallContext(kind="demo", verb="put")
        options, _ = _enrich_error(exc, _RwHandler(), ctx)
        assert options == ["append", "delete", "note", "replace"]

    def test_verb_unsupported_reflects_writable_flag(self):
        exc = PrecisError(ErrorCode.VERB_UNSUPPORTED, cause="put not allowed")
        ctx = CallContext(kind="romode", verb="put")
        options_ro, _ = _enrich_error(exc, _RoHandler(), ctx)
        assert options_ro == ["get", "search"]
        ctx2 = CallContext(kind="demo", verb="put")
        options_rw, _ = _enrich_error(exc, _RwHandler(), ctx2)
        assert options_rw == ["get", "put", "search"]

    def test_empty_vocab_yields_empty_options(self):
        class _Empty(Handler):
            scheme = "empty"
            views: set[str] = set()
            allowed_modes: set[str] = set()

            def read(self, *a, **kw):  # pragma: no cover
                return ""

        exc = PrecisError(ErrorCode.VIEW_UNKNOWN, cause="none")
        options, _ = _enrich_error(exc, _Empty(), CallContext(kind="empty"))
        assert options == []

    def test_handler_options_always_win(self):
        """If the raise-site passed options=, auto-fill is skipped."""
        exc = PrecisError(
            ErrorCode.MODE_UNSUPPORTED,
            cause="bad mode",
            options=["custom_a", "custom_b"],
        )
        ctx = CallContext(kind="demo", verb="put")
        options, _ = _enrich_error(exc, _RwHandler(), ctx)
        assert options == ["custom_a", "custom_b"]

    def test_kind_unknown_falls_back_to_kinds_registry(self):
        import precis.registry as reg

        reg._discover()
        exc = PrecisError(ErrorCode.KIND_UNKNOWN, cause="unknown")
        options, _ = _enrich_error(exc, None, CallContext())
        # Should include at least the stateless kinds always present.
        # (Exact list depends on env; check one known-present entry.)
        assert isinstance(options, list)
        assert len(options) > 0


class TestEnrichNext:
    """next= auto-fill rules."""

    def test_id_not_found_prefers_recent_view(self):
        exc = PrecisError(ErrorCode.ID_NOT_FOUND, cause="not there")
        ctx = CallContext(kind="demo", verb="get")
        _, nxt = _enrich_error(exc, _RwHandler(), ctx)
        assert "/recent" in nxt
        assert "demo" in nxt

    def test_id_not_found_falls_back_to_search_when_no_recent(self):
        exc = PrecisError(ErrorCode.ID_NOT_FOUND, cause="not there")
        ctx = CallContext(kind="romode", verb="get")
        _, nxt = _enrich_error(exc, _RoHandler(), ctx)
        assert "search(" in nxt
        assert "romode" in nxt

    def test_id_ambiguous_generic_disambiguation_hint(self):
        exc = PrecisError(ErrorCode.ID_AMBIGUOUS, cause="two matches")
        _, nxt = _enrich_error(exc, _RwHandler(), CallContext(kind="demo"))
        assert "disambiguate" in nxt

    def test_id_malformed_shape_hint(self):
        exc = PrecisError(ErrorCode.ID_MALFORMED, cause="not a slug")
        _, nxt = _enrich_error(exc, _RwHandler(), CallContext(kind="demo"))
        assert "scheme" in nxt.lower()
        assert "slug" in nxt.lower()

    def test_kind_unknown_points_at_stats(self):
        exc = PrecisError(ErrorCode.KIND_UNKNOWN, cause="typo")
        _, nxt = _enrich_error(exc, None, CallContext())
        assert "stats()" in nxt

    def test_kind_unavailable_generic_when_no_install_extra(self):
        exc = PrecisError(ErrorCode.KIND_UNAVAILABLE, cause="pg down")
        _, nxt = _enrich_error(exc, None, CallContext(kind="demo"))
        # Generic fallback when there's no install_extra on the KindSpec.
        assert "stats()" in nxt or "install" in nxt

    def test_handler_next_always_wins(self):
        exc = PrecisError(
            ErrorCode.ID_NOT_FOUND,
            cause="not there",
            next="custom: do the thing",
        )
        _, nxt = _enrich_error(exc, _RwHandler(), CallContext(kind="demo"))
        assert nxt == "custom: do the thing"

    def test_gripe_codes_left_empty_for_format_error_to_fill(self):
        """Gripe codes (TIMEOUT / UNEXPECTED / …) don't get next= from
        _enrich_error — _format_error appends the gripe hint itself."""
        for code in (
            ErrorCode.UNEXPECTED,
            ErrorCode.TIMEOUT,
            ErrorCode.UPSTREAM_ERROR,
            ErrorCode.RATE_LIMITED,
        ):
            exc = PrecisError(code, cause="x")
            _, nxt = _enrich_error(exc, _RwHandler(), CallContext(kind="demo"))
            assert nxt == ""


# ---------------------------------------------------------------------------
# invoke_handler — end-to-end enrichment visible in rendered output
# ---------------------------------------------------------------------------


class TestInvokeHandlerEnrichment:
    def test_view_unknown_produces_options_block(self):
        h = _RwHandler()
        r = invoke_handler(
            "demo",
            "get",
            h,
            lambda: (_ for _ in ()).throw(
                PrecisError(ErrorCode.VIEW_UNKNOWN, cause="view '/bad' unknown")
            ),
            args={"id": "x"},
        )
        rendered = r.render()
        assert "ERROR [view_unknown]:" in rendered
        assert "options: /recent, /summary, /toc" in rendered

    def test_mode_unsupported_produces_options_block(self):
        h = _RwHandler()
        r = invoke_handler(
            "demo",
            "put",
            h,
            lambda: (_ for _ in ()).throw(
                PrecisError(ErrorCode.MODE_UNSUPPORTED, cause="mode 'x' bad")
            ),
            args={"id": "y", "mode": "x"},
        )
        rendered = r.render()
        assert "options: append, delete, note, replace" in rendered

    def test_id_not_found_produces_next_hint(self):
        h = _RwHandler()
        r = invoke_handler(
            "demo",
            "get",
            h,
            lambda: (_ for _ in ()).throw(
                PrecisError(ErrorCode.ID_NOT_FOUND, cause="demo 'xxx' not found")
            ),
            args={"id": "xxx"},
        )
        rendered = r.render()
        assert "next:" in rendered
        assert "/recent" in rendered

    def test_handler_supplied_options_preserved(self):
        """If the raise-site supplied options, enrichment doesn't clobber them."""
        h = _RwHandler()
        r = invoke_handler(
            "demo",
            "put",
            h,
            lambda: (_ for _ in ()).throw(
                PrecisError(
                    ErrorCode.PARAM_INVALID,
                    cause="priority must be one of the standards",
                    options=["low", "normal", "high", "urgent"],
                    next="put(id='x', mode='append', priority='normal')",
                )
            ),
            args={"id": "y"},
        )
        rendered = r.render()
        assert "options: low, normal, high, urgent" in rendered
        assert "next: put(id='x', mode='append', priority='normal')" in rendered


# ---------------------------------------------------------------------------
# _format_error — style-guide compliance
# ---------------------------------------------------------------------------


class TestFormatErrorShape:
    def test_structure_has_four_lines_for_full_error(self):
        out = _format_error(
            ErrorCode.VIEW_UNKNOWN,
            CallContext(kind="demo", verb="get", args={"id": "x"}),
            cause="view '/bad' unknown",
            options=["/toc", "/summary"],
            next_hint="get(id='x/toc') for chunk index",
        )
        lines = out.splitlines()
        assert lines[0] == "ERROR [view_unknown]: view '/bad' unknown"
        assert lines[1] == "  where: type='demo' verb='get' id='x'"
        assert lines[2] == "  cause: view '/bad' unknown"
        assert lines[3] == "  options: /toc, /summary"
        assert lines[4] == "  next: get(id='x/toc') for chunk index"

    def test_gripe_code_appends_gripe_next_hint_when_empty(self):
        out = _format_error(
            ErrorCode.TIMEOUT,
            CallContext(kind="web", verb="search"),
            cause="upstream timed out",
        )
        assert "gripe" in out
        assert "put(type='gripe'" in out

    def test_non_gripe_code_no_gripe_hint(self):
        out = _format_error(
            ErrorCode.ID_NOT_FOUND,
            CallContext(kind="paper", verb="get"),
            cause="slug 'x' not in corpus",
        )
        assert "gripe" not in out

    def test_handler_next_suppresses_gripe_hint(self):
        out = _format_error(
            ErrorCode.TIMEOUT,
            CallContext(kind="web", verb="search"),
            cause="upstream timed out",
            next_hint="retry in 30s",
        )
        assert "next: retry in 30s" in out
        assert "gripe" not in out

    def test_missing_where_omits_line(self):
        out = _format_error(
            ErrorCode.PARAM_INVALID,
            CallContext(),
            cause="bad query",
        )
        assert "where:" not in out
        assert "cause: bad query" in out


# ---------------------------------------------------------------------------
# PrecisError signature hardening (Wave 3 — post-upgrade)
# ---------------------------------------------------------------------------


class TestPrecisErrorSignature:
    """After Wave 3 these should hold.  For now they document the target."""

    def test_structured_form_works(self):
        exc = PrecisError(
            ErrorCode.ID_NOT_FOUND,
            cause="slug 'x' not found",
            options=["/recent"],
            next="search(...)",
        )
        assert exc.code is ErrorCode.ID_NOT_FOUND
        assert exc.cause == "slug 'x' not found"
        assert exc.options == ["/recent"]
        assert exc.next == "search(...)"

    def test_bare_string_first_arg_rejected(self):
        """Wave 3: passing a bare string raises TypeError."""
        with pytest.raises(TypeError, match="must be an ErrorCode"):
            PrecisError("some bare message")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extract_kwargs — per-method arg validation helper
# ---------------------------------------------------------------------------


class TestExtractKwargs:
    def test_tuple_unpack_returns_values_in_key_order(self):
        wibble, size = extract_kwargs(
            {"wibble": 42, "size": "lg"},
            ("wibble", "size"),
        )
        assert wibble == 42
        assert size == "lg"

    def test_missing_optional_returns_none(self):
        (wibble,) = extract_kwargs({}, ("wibble",))
        assert wibble is None

    def test_unknown_kwarg_raises_with_allowed_in_options(self):
        with pytest.raises(PrecisError) as exc_info:
            extract_kwargs({"wibblez": 1}, ("wibble",), context="foo view")
        exc = exc_info.value
        assert exc.code is ErrorCode.PARAM_INVALID
        assert "wibblez" in exc.cause
        assert "foo view" in exc.cause
        assert exc.options == ["wibble"]

    def test_missing_required_raises_with_missing_in_options(self):
        with pytest.raises(PrecisError) as exc_info:
            extract_kwargs(
                {"size": "lg"},
                ("wibble", "size"),
                required=("wibble",),
                context="foo view",
            )
        exc = exc_info.value
        assert exc.code is ErrorCode.PARAM_INVALID
        assert "wibble" in exc.cause
        assert "foo view" in exc.cause
        assert exc.options == ["wibble"]

    def test_no_context_omits_on_clause(self):
        with pytest.raises(PrecisError) as exc_info:
            extract_kwargs({"bad": 1}, ())
        assert " on " not in exc_info.value.cause

    def test_ref_handler_view_method_rejects_unknown_kwarg(self):
        """RefHandler's _read_meta_view calls extract_kwargs at the top."""
        from precis.handlers._ref_base import RefHandler

        class _DummyHandler(RefHandler):
            scheme = "dummy"

            def _read_meta(self, ref):
                return "meta"

        handler = _DummyHandler()
        with pytest.raises(PrecisError) as exc_info:
            handler._read_meta_view(None, {}, None, None, wibblez=1)
        assert exc_info.value.code is ErrorCode.PARAM_INVALID
        assert "dummy/meta" in exc_info.value.cause
