"""Phase 2 tests — cost resolution, session stats, always-on footer, stats()."""

from __future__ import annotations

import pytest

from precis import server
from precis.protocol import CallContext, Handler, KindSpec, Plugin
from precis.registry import (
    ALIASES,
    KINDS,
    PLUGINS,
    SCHEMES,
    SESSION_STATS,
    CallStats,
    clear_kinds_mask,
    clear_session_stats,
    clear_startup_warnings,
    cost_hint_for,
    get_session_stats,
    invoke_handler,
    record_call,
    register_plugin,
)


class _EchoHandler(Handler):
    """Returns a fixed string; no overrides on cost_of/hints."""

    def read(self, path, selector, view, subview, query, summarize, depth, page):
        return "echo"


class _PaidHandler(Handler):
    """Handler that reports per-call cost dynamically."""

    def read(self, path, selector, view, subview, query, summarize, depth, page):
        return "paid"

    def cost_of(self, ctx: CallContext) -> str | None:
        return "~$0.005/call"


# ---------------------------------------------------------------------------
# Three-level cost fallback — cost_hint_for()
# ---------------------------------------------------------------------------


class TestCostHintFor:
    def test_per_call_overrides_everything(self):
        # Even when paper's KindSpec says 'free', a per-call value wins.
        assert cost_hint_for("paper", "~$0.01/call") == "~$0.01/call"

    def test_falls_back_to_kindspec_cost_hint(self):
        # Papers builtin has cost_hint='free'.
        assert cost_hint_for("paper", None) == "free"

    def test_ultimate_default_when_kind_unknown(self):
        # Unregistered kind → still produces a footer.
        assert cost_hint_for("fruitbat", None) == "free"

    def test_empty_per_call_treated_as_missing(self):
        # Empty string is falsy; falls through to static/default.
        assert cost_hint_for("paper", "") == "free"


# ---------------------------------------------------------------------------
# Session-stats accumulator
# ---------------------------------------------------------------------------


class TestRecordCall:
    def setup_method(self):
        clear_session_stats()

    def teardown_method(self):
        clear_session_stats()

    def test_first_call_creates_entry(self):
        record_call("paper", "free")
        stats = get_session_stats()
        assert "paper" in stats
        assert stats["paper"].calls == 1
        assert stats["paper"].errors == 0
        assert stats["paper"].last_cost == "free"

    def test_multiple_calls_increment_count(self):
        for _ in range(5):
            record_call("paper", "free")
        assert get_session_stats()["paper"].calls == 5

    def test_error_call_bumps_both_counters(self):
        record_call("paper", "free")
        record_call("paper", "free", errored=True)
        stats = get_session_stats()["paper"]
        assert stats.calls == 2
        assert stats.errors == 1

    def test_last_cost_overwrites(self):
        record_call("web", "~$0.002/call")
        record_call("web", "~$0.003/call")
        assert get_session_stats()["web"].last_cost == "~$0.003/call"

    def test_empty_cost_falls_back_to_free(self):
        record_call("paper", "")
        assert get_session_stats()["paper"].last_cost == "free"

    def test_get_session_stats_returns_copy(self):
        record_call("paper", "free")
        got = get_session_stats()
        got["fruitbat"] = CallStats(calls=99)
        assert "fruitbat" not in SESSION_STATS


# ---------------------------------------------------------------------------
# invoke_handler integration — stats recorded, cost footer always on
# ---------------------------------------------------------------------------


class TestInvokeHandlerCostAndStats:
    def setup_method(self):
        clear_session_stats()

    def teardown_method(self):
        clear_session_stats()

    def test_success_records_call(self):
        h = _EchoHandler()
        invoke_handler("paper", "get", h, lambda: "ok", args={"id": "x"})
        stats = get_session_stats()
        assert stats["paper"].calls == 1
        assert stats["paper"].errors == 0

    def test_error_records_call_with_error_flag(self):
        h = _EchoHandler()
        invoke_handler(
            "paper",
            "get",
            h,
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            args={"id": "x"},
        )
        stats = get_session_stats()
        assert stats["paper"].calls == 1
        assert stats["paper"].errors == 1

    def test_paid_handler_cost_flows_through(self):
        h = _PaidHandler()
        result = invoke_handler("paper", "get", h, lambda: "ok", args={"id": "x"})
        assert result.cost == "~$0.005/call"
        assert get_session_stats()["paper"].last_cost == "~$0.005/call"

    def test_default_handler_cost_is_free_via_kindspec(self):
        # paper's KindSpec sets cost_hint='free' (Phase 1 annotation).
        h = _EchoHandler()
        result = invoke_handler("paper", "get", h, lambda: "ok", args={"id": "x"})
        assert result.cost == "free"

    def test_render_always_emits_footer_on_success(self):
        h = _EchoHandler()
        result = invoke_handler(
            "paper", "get", h, lambda: "paper body", args={"id": "x"}
        )
        rendered = result.render()
        assert rendered.rstrip().endswith("[cost: free]")


# ---------------------------------------------------------------------------
# Server tool dispatch — tools go through _dispatch, every response footed
# ---------------------------------------------------------------------------


@pytest.fixture
def paper_mock(monkeypatch):
    """Patch tools.read so we can call server tools in isolation."""
    calls: list[dict] = []

    def fake_read(uri="", query="", page=1, top_k=5, depth=0):
        calls.append({"uri": uri, "query": query})
        return f"[paper body for {uri}]"

    def fake_put(**kwargs):
        calls.append(kwargs)
        return f"[wrote to {kwargs.get('uri')}]"

    monkeypatch.setattr("precis.server.tools.read", fake_read)
    monkeypatch.setattr("precis.server.tools.put", fake_put)
    clear_session_stats()
    clear_kinds_mask()
    yield calls
    clear_session_stats()


class TestServerDispatchFooter:
    def test_search_response_carries_cost_footer(self, paper_mock):
        out = server.search(query="CO2 capture", scope="wang2020state")
        assert "[paper body for paper:wang2020state]" in out
        assert "[cost: free]" in out

    def test_get_response_carries_cost_footer(self, paper_mock):
        out = server.get(id="wang2020state")
        assert "[paper body" in out
        assert "[cost: free]" in out

    def test_put_response_carries_cost_footer(self, paper_mock):
        # put() dispatches through _dispatch too.
        out = server.put(id="wang2020state", text="a note", mode="note")
        assert "[wrote to" in out
        assert "[cost: free]" in out

    def test_move_response_carries_cost_footer(self, paper_mock):
        out = server.move(id="wang2020state", after="foo")
        # Papers don't actually support move, but the underlying tools.put
        # is mocked so _dispatch sees a successful callable.
        assert "[cost: free]" in out

    def test_session_stats_accumulate_across_calls(self, paper_mock):
        assert get_session_stats() == {}
        server.get(id="wang2020state")
        server.get(id="smith2021fwd")
        # ``search`` now requires explicit ``type=`` when no scope is
        # given — the old silent paper-default was removed because it
        # caused silent cross-kind leaks (see smoke-test §6.3 / §15.2).
        server.search(query="MOF", type="paper")
        stats = get_session_stats()
        # Three calls on 'paper' kind — canonical for all three.
        assert stats["paper"].calls == 3
        assert stats["paper"].last_cost == "free"


# ---------------------------------------------------------------------------
# stats() tool — session section rendering
# ---------------------------------------------------------------------------


class TestStatsSessionSection:
    def setup_method(self):
        clear_session_stats()
        clear_kinds_mask()
        clear_startup_warnings()

    def teardown_method(self):
        clear_session_stats()
        clear_kinds_mask()
        clear_startup_warnings()

    def test_stats_shows_no_calls_when_empty(self):
        out = server.stats()
        assert "session: (no calls yet)" in out

    def test_stats_lists_each_kind_that_was_called(self):
        record_call("paper", "free")
        record_call("paper", "free")
        record_call("web", "~$0.002/call")
        out = server.stats()
        assert "session:" in out
        assert "paper" in out
        assert "calls=2" in out
        assert "web" in out
        assert "last_cost=~$0.002/call" in out

    def test_stats_sorts_session_entries(self):
        # Reverse insertion order — 'zeta' first, 'alpha' last.
        record_call("zeta", "free")
        record_call("alpha", "free")
        out = server.stats()
        lines = out.splitlines()
        zeta_idx = next(i for i, line in enumerate(lines) if "zeta" in line)
        alpha_idx = next(i for i, line in enumerate(lines) if "alpha" in line)
        # Alphabetical order → alpha before zeta in output.
        assert alpha_idx < zeta_idx

    def test_stats_shows_error_count_when_present(self):
        record_call("paper", "free")
        record_call("paper", "free", errored=True)
        record_call("paper", "free", errored=True)
        out = server.stats()
        # Find the paper line.
        paper_line = next(
            line for line in out.splitlines() if "paper" in line and "calls=" in line
        )
        assert "calls=3" in paper_line
        assert "errors=2" in paper_line


# ---------------------------------------------------------------------------
# Paid kind — custom cost_hint flows through Render footer
# ---------------------------------------------------------------------------


class TestPaidKindFooter:
    def setup_method(self):
        # Register a temporary paid kind.
        plugin = Plugin(
            name="paid-test",
            handler_cls=_PaidHandler,
            schemes=["paidtest"],
            kinds=[
                KindSpec(
                    name="paidtest",
                    description="Paid test kind",
                    cost_hint="~$0.01/call",  # static fallback
                )
            ],
        )
        register_plugin(plugin)
        clear_session_stats()

    def teardown_method(self):
        KINDS.pop("paidtest", None)
        SCHEMES.pop("paidtest", None)
        PLUGINS.pop("paid-test", None)
        ALIASES.pop("paidtest", None)
        clear_session_stats()

    def test_per_call_cost_beats_static(self):
        # The handler returns '~$0.005/call' from cost_of(); that wins.
        h = _PaidHandler()
        result = invoke_handler("paidtest", "get", h, lambda: "body", args={})
        assert result.cost == "~$0.005/call"

    def test_static_fallback_when_cost_of_returns_none(self):
        class _Silent(_PaidHandler):
            def cost_of(self, ctx):
                return None

        h = _Silent()
        result = invoke_handler("paidtest", "get", h, lambda: "body", args={})
        # Falls back to the KindSpec's static '~$0.01/call'.
        assert result.cost == "~$0.01/call"
