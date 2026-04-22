"""Tests for invoke_handler() wrapper + _format_error (Phase 0, §11.1, §11.2)."""

from __future__ import annotations

from precis.protocol import (
    CallContext,
    ErrorCode,
    Handler,
    HintContext,
    PrecisError,
)
from precis.registry import _aggregate_hints, _format_error, invoke_handler

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubHandler(Handler):
    """Configurable stub for wrapper tests.  Read/put never called directly —
    invoke_handler consumes the zero-arg callable we pass in."""

    scheme = "stub"

    def __init__(
        self,
        hints: list[str] | None = None,
        cost: str | None = None,
        hints_raises: bool = False,
        cost_raises: bool = False,
    ):
        self._hints = hints or []
        self._cost = cost
        self._hints_raises = hints_raises
        self._cost_raises = cost_raises

    def read(self, path, selector, view, subview, query, summarize, depth, page):
        return ""

    def hints(self, result, ctx: HintContext) -> list[str]:
        if self._hints_raises:
            raise RuntimeError("hints blew up")
        return list(self._hints)

    def cost_of(self, ctx: CallContext) -> str | None:
        if self._cost_raises:
            raise RuntimeError("cost blew up")
        return self._cost


# ---------------------------------------------------------------------------
# _format_error — shape per §11.2
# ---------------------------------------------------------------------------


def test_format_error_minimum_is_code_plus_one_line_cause():
    ctx = CallContext(kind="paper", verb="get", args={"id": "wang2020state"})
    out = _format_error(
        ErrorCode.ID_NOT_FOUND,
        ctx,
        cause="paper 'wang2020state' not in corpus",
    )
    assert out.splitlines()[0] == (
        "ERROR [id_not_found]: paper 'wang2020state' not in corpus"
    )


def test_format_error_where_line_uses_type_verb_id():
    ctx = CallContext(kind="paper", verb="get", args={"id": "wang2020state"})
    out = _format_error(ErrorCode.VIEW_UNKNOWN, ctx, cause="unknown view '/histogram'")
    assert "  where: type='paper' verb='get' id='wang2020state'" in out


def test_format_error_omits_where_line_when_no_context():
    # No kind, no verb, no id → no where line.
    ctx = CallContext()
    out = _format_error(ErrorCode.UNEXPECTED, ctx, cause="booted without ctx")
    assert "where:" not in out


def test_format_error_includes_options_when_provided():
    ctx = CallContext(kind="paper", verb="get", args={"id": "x"})
    out = _format_error(
        ErrorCode.VIEW_UNKNOWN,
        ctx,
        cause="unknown view '/histogram'",
        options=["/summary", "/toc", "/meta"],
    )
    assert "  options: /summary, /toc, /meta" in out


def test_format_error_skips_empty_options_line():
    ctx = CallContext(kind="paper", verb="get", args={"id": "x"})
    out = _format_error(ErrorCode.ID_NOT_FOUND, ctx, cause="gone", options=None)
    assert "options:" not in out


def test_format_error_uses_explicit_next_hint_when_provided():
    ctx = CallContext(kind="web", verb="search", args={"id": ""})
    out = _format_error(
        ErrorCode.RATE_LIMITED,
        ctx,
        cause="perplexity returned 429",
        next_hint="retry in 30s",
    )
    assert "  next: retry in 30s" in out
    # Explicit next overrides the auto-gripe hint even on gripe-worthy codes.
    assert "gripe" not in out


def test_format_error_auto_adds_gripe_next_hint_for_non_user_errors():
    ctx = CallContext(kind="web", verb="search", args={"id": ""})
    out = _format_error(ErrorCode.UNEXPECTED, ctx, cause="KeyError: 'choices'")
    assert "  next:" in out
    assert "gripe about it" in out
    assert "put(type='gripe'" in out


def test_format_error_does_not_add_gripe_hint_for_user_errors():
    ctx = CallContext(kind="paper", verb="get", args={"id": "x"})
    for code in (
        ErrorCode.VIEW_UNKNOWN,
        ErrorCode.ID_NOT_FOUND,
        ErrorCode.PARAM_INVALID,
        ErrorCode.VERB_UNSUPPORTED,
        ErrorCode.READONLY,
    ):
        out = _format_error(code, ctx, cause="user-side mistake")
        assert "gripe" not in out, f"gripe hint wrongly added for {code}"


def test_format_error_accepts_raw_string_code():
    # Callers can pass a raw string code for forward compatibility.
    ctx = CallContext(kind="x", verb="get")
    out = _format_error("custom_code", ctx, cause="something")
    assert out.startswith("ERROR [custom_code]:")


def test_format_error_summary_takes_first_line_of_multiline_cause():
    ctx = CallContext(kind="paper", verb="get", args={"id": "x"})
    out = _format_error(
        ErrorCode.ID_NOT_FOUND,
        ctx,
        cause="first line\nsecond line\nthird line",
    )
    # Summary line only uses first line.
    assert out.splitlines()[0] == "ERROR [id_not_found]: first line"
    # Full cause block still appears below.
    assert "first line\nsecond line\nthird line" in out


# ---------------------------------------------------------------------------
# _aggregate_hints — dedup, cap, error-swallowing
# ---------------------------------------------------------------------------


def test_aggregate_hints_dedupes_preserving_order():
    h = _StubHandler(hints=["a", "b", "a", "c", "b"])
    ctx = HintContext(call=CallContext(kind="x", verb="get"), result_count=1)
    assert _aggregate_hints(h, None, ctx) == ["a", "b", "c"]


def test_aggregate_hints_filters_empty_and_non_string_entries():
    h = _StubHandler(hints=["", "  ", "real hint", None, 42, "another"])  # type: ignore[list-item]
    ctx = HintContext(call=CallContext(kind="x", verb="get"), result_count=1)
    assert _aggregate_hints(h, None, ctx) == ["real hint", "another"]


def test_aggregate_hints_caps_at_five():
    h = _StubHandler(hints=[f"hint {i}" for i in range(10)])
    ctx = HintContext(call=CallContext(kind="x", verb="get"), result_count=1)
    got = _aggregate_hints(h, None, ctx)
    assert len(got) == 5
    assert got == ["hint 0", "hint 1", "hint 2", "hint 3", "hint 4"]


def test_aggregate_hints_swallows_handler_exceptions():
    # A crashing handler.hints() must never kill the response.
    h = _StubHandler(hints_raises=True)
    ctx = HintContext(call=CallContext(kind="x", verb="get"), result_count=1)
    assert _aggregate_hints(h, None, ctx) == []


# ---------------------------------------------------------------------------
# invoke_handler — success + failure paths
# ---------------------------------------------------------------------------


def test_invoke_handler_success_wraps_result_with_hints_and_cost():
    h = _StubHandler(hints=["try /summary"], cost="free")
    r = invoke_handler(
        "paper", "get", h, lambda: "hello world", args={"id": "wang2020state"}
    )
    assert r.success
    assert r.data == "hello world"
    assert r.kind == "paper"
    assert r.cost == "free"
    assert r.hints == ["try /summary"]


def test_invoke_handler_precis_error_becomes_formatted_result():
    h = _StubHandler()
    r = invoke_handler(
        "paper",
        "get",
        h,
        lambda: (_ for _ in ()).throw(
            PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause="paper 'xxx' not in corpus",
                next="try search(query='...')",
            )
        ),
        args={"id": "xxx"},
    )
    assert not r.success
    rendered = r.render()
    assert rendered.startswith("ERROR [id_not_found]:")
    assert "type='paper' verb='get' id='xxx'" in rendered
    assert "paper 'xxx' not in corpus" in rendered
    assert "next: try search(query='...')" in rendered


def test_invoke_handler_legacy_string_precis_error_defaults_to_unexpected():
    h = _StubHandler()
    r = invoke_handler(
        "paper",
        "get",
        h,
        lambda: (_ for _ in ()).throw(PrecisError("boom")),
        args={"id": "x"},
    )
    assert not r.success
    # Legacy form carries no code → unexpected.
    assert "ERROR [unexpected]:" in r.error
    # …and gets a gripe-hint because unexpected is a non-user error.
    assert "gripe" in r.error


def test_invoke_handler_unexpected_exception_becomes_unexpected_result():
    h = _StubHandler()
    r = invoke_handler(
        "web",
        "search",
        h,
        lambda: (_ for _ in ()).throw(ValueError("bad json")),
        args={"id": ""},
    )
    assert not r.success
    assert "ERROR [unexpected]:" in r.error
    assert "ValueError: bad json" in r.error
    # Non-user error → gripe hint.
    assert "gripe about it" in r.error


def test_invoke_handler_cost_of_exception_does_not_break_success():
    # A crashing cost_of() must not turn a success into a failure.
    h = _StubHandler(hints=["ok"], cost_raises=True)
    r = invoke_handler("paper", "get", h, lambda: "data", args={"id": "x"})
    assert r.success
    assert r.data == "data"
    # Phase 2: a crashing cost_of() falls back to the KindSpec hint or
    # the ultimate default 'free' via cost_hint_for().  The wrapper never
    # exposes a None-cost Result to the agent — the footer is always on.
    assert r.cost == "free"
    assert r.hints == ["ok"]


def test_invoke_handler_hints_exception_does_not_break_success():
    h = _StubHandler(hints_raises=True, cost="$0.01")
    r = invoke_handler("web", "search", h, lambda: ["r1", "r2"], args={})
    assert r.success
    assert r.data == ["r1", "r2"]
    assert r.hints == []
    assert r.cost == "$0.01"


def test_invoke_handler_passes_kind_verb_to_error_where_line():
    h = _StubHandler()
    r = invoke_handler(
        "memory",
        "put",
        h,
        lambda: (_ for _ in ()).throw(ValueError("db down")),
        args={"id": "drawer-abc"},
    )
    assert "type='memory' verb='put' id='drawer-abc'" in r.error


def test_invoke_handler_result_renders_with_hints_block():
    h = _StubHandler(hints=["hint1", "hint2"], cost="~$0.002/call")
    r = invoke_handler("web", "search", h, lambda: "result text", args={"id": ""})
    out = r.render()
    assert out.startswith("result text")
    assert "Hints:" in out
    assert "  - hint1" in out
    assert "  - hint2" in out
    assert "[cost: ~$0.002/call]" in out
