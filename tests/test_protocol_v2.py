"""Tests for Plugin protocol v2 additions (Phase 0, docs/plugin-architecture.md §16)."""

from __future__ import annotations

from dataclasses import is_dataclass

import pytest

from precis.protocol import (
    GRIPE_HINT_CODES,
    PLUGIN_PROTOCOL_VERSION,
    CallContext,
    ErrorCode,
    Handler,
    HintContext,
    KindSpec,
    NotificationContext,
    Plugin,
    PrecisError,
    Result,
)

# ---------------------------------------------------------------------------
# Protocol version
# ---------------------------------------------------------------------------


def test_protocol_version_constant_is_major_only_semver():
    assert isinstance(PLUGIN_PROTOCOL_VERSION, str)
    parts = PLUGIN_PROTOCOL_VERSION.split(".")
    # Phase 0 ships v1 — just the major digit.
    assert parts[0] == "1"
    # Tolerate future minor/patch additions but reject nonsense.
    for bit in parts:
        assert bit.isdigit()


# ---------------------------------------------------------------------------
# ErrorCode enum
# ---------------------------------------------------------------------------


def test_error_code_has_all_sixteen_standard_codes():
    # Locked catalogue per §11.3.
    expected = {
        "kind_unknown",
        "kind_unavailable",
        "verb_unsupported",
        "view_unknown",
        "mode_unsupported",
        "id_not_found",
        "id_ambiguous",
        "id_malformed",
        "param_invalid",
        "readonly",
        "denied",
        "timeout",
        "rate_limited",
        "upstream_error",
        "unavailable",
        "unexpected",
    }
    assert {c.value for c in ErrorCode} == expected


def test_error_code_values_are_strings_for_serialisation():
    # ErrorCode is (str, Enum), so members compare equal to their string value.
    assert ErrorCode.TIMEOUT == "timeout"
    assert ErrorCode.UNEXPECTED.value == "unexpected"


def test_gripe_hint_codes_covers_only_non_user_errors():
    # Agent's fault → no gripe hint.
    assert ErrorCode.VIEW_UNKNOWN not in GRIPE_HINT_CODES
    assert ErrorCode.ID_NOT_FOUND not in GRIPE_HINT_CODES
    assert ErrorCode.PARAM_INVALID not in GRIPE_HINT_CODES
    # Not agent's fault → gripe hint.
    assert ErrorCode.UNEXPECTED in GRIPE_HINT_CODES
    assert ErrorCode.TIMEOUT in GRIPE_HINT_CODES
    assert ErrorCode.UPSTREAM_ERROR in GRIPE_HINT_CODES
    assert ErrorCode.RATE_LIMITED in GRIPE_HINT_CODES
    assert ErrorCode.UNAVAILABLE in GRIPE_HINT_CODES


# ---------------------------------------------------------------------------
# PrecisError — structured + legacy string compatibility
# ---------------------------------------------------------------------------


def test_precis_error_bare_string_first_arg_rejected():
    # Wave 3: bare-string form removed — TypeError at construction.
    with pytest.raises(TypeError, match="must be an ErrorCode"):
        PrecisError("todo slug required for replace")  # type: ignore[arg-type]


def test_precis_error_structured_form_carries_fields():
    exc = PrecisError(
        ErrorCode.ID_NOT_FOUND,
        cause="paper 'wang2020state' not in corpus",
        options=["search(query='wang 2020')", "check spelling"],
        next="try search(query='...')",
    )
    assert exc.code is ErrorCode.ID_NOT_FOUND
    assert exc.cause == "paper 'wang2020state' not in corpus"
    assert exc.options == ["search(query='wang 2020')", "check spelling"]
    assert exc.next == "try search(query='...')"


def test_precis_error_options_is_copied_not_shared():
    # Paranoid: callers should not be able to mutate the exception's list.
    opts = ["a", "b"]
    exc = PrecisError(ErrorCode.ID_AMBIGUOUS, cause="two hits", options=opts)
    opts.append("c")
    assert exc.options == ["a", "b"]


def test_precis_error_is_still_an_exception():
    with pytest.raises(PrecisError) as excinfo:
        raise PrecisError(ErrorCode.TIMEOUT, cause="handler took too long")
    assert excinfo.value.code is ErrorCode.TIMEOUT


# ---------------------------------------------------------------------------
# KindSpec
# ---------------------------------------------------------------------------


def test_kindspec_minimum_fields():
    spec = KindSpec(name="websearch", description="Quick web synthesis")
    assert spec.name == "websearch"
    assert spec.description == "Quick web synthesis"
    assert spec.aliases == []
    assert spec.requires == []
    assert spec.cost_hint is None
    assert spec.examples == []


def test_kindspec_aliases_and_requires_are_independent_lists():
    a = KindSpec(name="websearch", description="x")
    b = KindSpec(name="think", description="y")
    a.aliases.append("perplexity")
    assert b.aliases == []  # not shared default


def test_kindspec_is_a_dataclass():
    assert is_dataclass(KindSpec)


# ---------------------------------------------------------------------------
# CallContext / HintContext / NotificationContext
# ---------------------------------------------------------------------------


def test_call_context_started_auto_populates():
    ctx = CallContext(kind="paper", verb="get")
    # started is a monotonic time — should be > 0 and <= now.
    assert ctx.started > 0
    # elapsed_s returns a non-negative float.
    assert ctx.elapsed_s >= 0


def test_call_context_args_default_is_isolated_per_instance():
    a = CallContext(kind="paper", verb="get")
    b = CallContext(kind="paper", verb="get")
    a.args["id"] = "foo"
    assert b.args == {}


def test_hint_context_from_result_counts_lists():
    ctx = CallContext(kind="paper", verb="search")
    hint_ctx = HintContext.from_result([1, 2, 3, 4], ctx)
    assert hint_ctx.result_count == 4
    assert hint_ctx.call is ctx


def test_hint_context_from_result_unwraps_dict_payloads():
    ctx = CallContext(kind="paper", verb="search")
    assert HintContext.from_result({"items": [1, 2]}, ctx).result_count == 2
    assert HintContext.from_result({"results": [1, 2, 3]}, ctx).result_count == 3
    assert HintContext.from_result({"hits": []}, ctx).result_count == 0


def test_hint_context_from_result_leaves_count_none_for_scalar_results():
    ctx = CallContext(kind="paper", verb="get")
    assert HintContext.from_result("some rendered text", ctx).result_count is None
    assert HintContext.from_result({"no_known_key": "x"}, ctx).result_count is None


def test_notification_context_defaults():
    nctx = NotificationContext()
    assert nctx.agent_id == ""
    assert nctx.kinds_mask == frozenset()


# ---------------------------------------------------------------------------
# Plugin — new kinds field + protocol_version
# ---------------------------------------------------------------------------


class _DummyHandler(Handler):
    """A no-op handler used only for plugin-level tests."""

    def read(
        self,
        path,
        selector,
        view,
        subview,
        query,
        summarize,
        depth,
        page,
    ) -> str:
        return ""


def test_plugin_protocol_version_defaults_to_current():
    p = Plugin(name="x", handler_cls=_DummyHandler, schemes=["x"])
    assert p.protocol_version == PLUGIN_PROTOCOL_VERSION


def test_plugin_kinds_defaults_empty_and_is_independent_per_instance():
    a = Plugin(name="a", handler_cls=_DummyHandler, schemes=["a"])
    b = Plugin(name="b", handler_cls=_DummyHandler, schemes=["b"])
    assert a.kinds == []
    a.kinds.append(KindSpec(name="a", description="..."))
    assert b.kinds == []


# ---------------------------------------------------------------------------
# Handler — optional hooks with safe defaults
# ---------------------------------------------------------------------------


def test_handler_cost_of_default_returns_none():
    h = _DummyHandler()
    assert h.cost_of(CallContext(kind="x", verb="get")) is None


def test_handler_hints_default_returns_empty_list():
    h = _DummyHandler()
    ctx = HintContext(call=CallContext(kind="x", verb="get"), result_count=0)
    assert h.hints("anything", ctx) == []


def test_handler_notifications_default_returns_empty_list():
    h = _DummyHandler()
    assert h.notifications(NotificationContext()) == []


# ---------------------------------------------------------------------------
# Result — ok / err / render()
# ---------------------------------------------------------------------------


def test_result_ok_carries_data_and_optional_footer_fields():
    r = Result.ok(
        "hello", kind="websearch", cost="~$0.002/call", hints=["try type='think'"]
    )
    assert r.success is True
    assert r.data == "hello"
    assert r.kind == "websearch"
    assert r.cost == "~$0.002/call"
    assert r.hints == ["try type='think'"]


def test_result_err_carries_error_and_is_not_success():
    r = Result.err("ERROR [timeout]: exceeded 30s")
    assert r.success is False
    assert r.error.startswith("ERROR [timeout]")


def test_result_render_error_is_pass_through():
    r = Result.err("ERROR [view_unknown]: nope\n  options: /toc, /summary")
    assert r.render() == "ERROR [view_unknown]: nope\n  options: /toc, /summary"


def test_result_render_success_with_hints_and_cost():
    r = Result.ok(
        "Paper text here.",
        kind="paper",
        cost="free",
        hints=["try /summary for a digest", "try /fig for figures"],
    )
    out = r.render()
    assert out.startswith("Paper text here.")
    assert "Hints:" in out
    assert "  - try /summary for a digest" in out
    assert "  - try /fig for figures" in out
    assert out.rstrip().endswith("[cost: free]")


def test_result_render_success_plain_when_no_hints_or_cost():
    r = Result.ok("Just the data.", kind="paper")
    assert r.render() == "Just the data."


def test_result_render_handles_non_string_data():
    r = Result.ok({"items": [1, 2]}, kind="paper")
    # Falls back to str() — never crashes.
    assert "items" in r.render()


def test_result_ok_hints_is_isolated_per_instance():
    r1 = Result.ok("a", kind="x")
    r2 = Result.ok("b", kind="x")
    r1.hints.append("hint")
    assert r2.hints == []
