"""Tests for :mod:`precis.kind_gate` + the banner integration.

Phase 4 of the cold-start token budget design
(``docs/design/mcp-cold-start-token-budget.md``). Covers:

- :func:`precis.kind_gate.parse_disabled` parsing of the env value.
- :class:`precis.kind_gate.Loadability` invariants.
- :func:`precis.kind_gate.gate` prohibition + resources_present.
- :func:`precis.kind_gate.loadability_from_exception` translation.
- :func:`precis.kind_gate.format_unavailable` banner rendering.
- :func:`precis.server._kinds_unavailable_line` integration via a
  fake hub carrying explicit :class:`Loadability` verdicts.
- ``PatentHandler.__init__`` convergence: env trio reading happens
  inside the handler, so missing envs raise InitError with the
  conventional ``"patent: missing env vars ..."`` shape.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from precis.kind_gate import (
    Loadability,
    format_unavailable,
    gate,
    loadability_from_exception,
    parse_disabled,
)
from precis.protocol import KindSpec

# ---------------------------------------------------------------------------
# parse_disabled()
# ---------------------------------------------------------------------------


def test_parse_disabled_handles_none() -> None:
    assert parse_disabled(None) == frozenset()


def test_parse_disabled_handles_empty_string() -> None:
    assert parse_disabled("") == frozenset()


def test_parse_disabled_handles_single_kind() -> None:
    assert parse_disabled("patent") == frozenset({"patent"})


def test_parse_disabled_handles_multiple_kinds() -> None:
    assert parse_disabled("patent,web,youtube") == frozenset(
        {"patent", "web", "youtube"}
    )


def test_parse_disabled_tolerates_whitespace() -> None:
    assert parse_disabled(" patent ,  web  ") == frozenset({"patent", "web"})


def test_parse_disabled_dedupes() -> None:
    assert parse_disabled("patent,web,patent") == frozenset({"patent", "web"})


def test_parse_disabled_drops_empty_entries() -> None:
    assert parse_disabled("patent,,web,") == frozenset({"patent", "web"})


# ---------------------------------------------------------------------------
# Loadability invariants
# ---------------------------------------------------------------------------


def test_loadability_loaded_true_rejects_reason() -> None:
    with pytest.raises(ValueError, match="must not carry a reason"):
        Loadability(kind="foo", loaded=True, reason="oops")


def test_loadability_loaded_false_requires_reason() -> None:
    with pytest.raises(ValueError, match="must carry a non-empty reason"):
        Loadability(kind="foo", loaded=False, reason=None)


def test_loadability_loaded_true_no_reason() -> None:
    v = Loadability(kind="foo", loaded=True)
    assert v.loaded is True
    assert v.reason is None


def test_loadability_loaded_false_with_reason() -> None:
    v = Loadability(kind="foo", loaded=False, reason="prohibited")
    assert v.loaded is False
    assert v.reason == "prohibited"


# ---------------------------------------------------------------------------
# gate()
# ---------------------------------------------------------------------------


def _spec(kind: str, requires_env: tuple[str, ...] = ()) -> KindSpec:
    return KindSpec(
        kind=kind,
        title=kind.title(),
        description=f"{kind} kind for tests",
        requires_env=requires_env,
    )


def test_gate_passes_clean_spec() -> None:
    """No prohibition, no env requirements → loaded=True."""
    v = gate(_spec("foo"), disabled=frozenset())
    assert v.loaded is True
    assert v.reason is None


def test_gate_prohibits_listed_kind() -> None:
    v = gate(_spec("foo"), disabled=frozenset({"foo"}))
    assert v.loaded is False
    assert v.reason == "prohibited"


def test_gate_prohibition_wins_over_missing_envs() -> None:
    """Operator intent (prohibition) wins over resource availability."""
    v = gate(
        _spec("foo", requires_env=("FOO_KEY",)),
        disabled=frozenset({"foo"}),
    )
    assert v.loaded is False
    assert v.reason == "prohibited"


def test_gate_reports_single_missing_env() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FOO_KEY", None)
        v = gate(_spec("foo", requires_env=("FOO_KEY",)), disabled=frozenset())
    assert v.loaded is False
    assert v.reason == "missing FOO_KEY"


def test_gate_reports_multiple_missing_envs() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FOO_KEY", None)
        os.environ.pop("BAR_TOKEN", None)
        v = gate(
            _spec("foo", requires_env=("FOO_KEY", "BAR_TOKEN")),
            disabled=frozenset(),
        )
    assert v.loaded is False
    assert v.reason == "missing FOO_KEY, BAR_TOKEN"


def test_gate_treats_empty_env_value_as_missing() -> None:
    """``FOO_KEY=`` (set but empty) counts as missing — pydantic-
    settings does the same, and `os.environ.get(name)` returning
    ``''`` is the canonical "not configured" signal.
    """
    with patch.dict(os.environ, {"FOO_KEY": ""}, clear=False):
        v = gate(_spec("foo", requires_env=("FOO_KEY",)), disabled=frozenset())
    assert v.loaded is False
    assert v.reason == "missing FOO_KEY"


def test_gate_passes_when_envs_present() -> None:
    with patch.dict(os.environ, {"FOO_KEY": "x", "BAR_TOKEN": "y"}, clear=False):
        v = gate(
            _spec("foo", requires_env=("FOO_KEY", "BAR_TOKEN")),
            disabled=frozenset(),
        )
    assert v.loaded is True


# ---------------------------------------------------------------------------
# loadability_from_exception()
# ---------------------------------------------------------------------------


def test_loadability_from_exception_strips_kind_prefix() -> None:
    """InitError convention: ``"<kind>: <reason>"``. The kind prefix
    is redundant on the banner (the entry is already keyed by kind).
    """
    exc = RuntimeError("paper: store required")
    v = loadability_from_exception(_spec("paper"), exc)
    assert v.loaded is False
    assert v.reason == "store required"


def test_loadability_from_exception_keeps_non_prefixed_message() -> None:
    """A plain message without the kind-prefix passes through verbatim."""
    exc = ImportError("No module named 'sympy'")
    v = loadability_from_exception(_spec("calc"), exc)
    assert v.loaded is False
    assert v.reason == "No module named 'sympy'"


def test_loadability_from_exception_truncates_long_message() -> None:
    """Banner lines stay readable; long messages truncate to 60 chars."""
    very_long = "x" * 200
    exc = RuntimeError(very_long)
    v = loadability_from_exception(_spec("foo"), exc)
    assert v.reason is not None
    assert len(v.reason) <= 60
    assert v.reason.endswith("...")


def test_loadability_from_exception_falls_back_to_type_name() -> None:
    """An exception with an empty message still produces a useful
    reason via the type name."""
    exc = ValueError()
    v = loadability_from_exception(_spec("foo"), exc)
    assert v.reason == "ValueError"


# ---------------------------------------------------------------------------
# format_unavailable()
# ---------------------------------------------------------------------------


def test_format_unavailable_empty_when_all_loaded() -> None:
    """Every recorded verdict is loaded=True → no banner line."""
    verdicts = {
        "paper": Loadability("paper", True),
        "memory": Loadability("memory", True),
    }
    assert format_unavailable(verdicts) == ""


def test_format_unavailable_single_entry() -> None:
    verdicts = {"patent": Loadability("patent", False, "prohibited")}
    assert format_unavailable(verdicts) == "Kinds unavailable: patent (prohibited)."


def test_format_unavailable_sorts_alphabetically() -> None:
    """Stable ordering across boots so the banner doesn't churn."""
    verdicts = {
        "patent": Loadability("patent", False, "prohibited"),
        "math": Loadability("math", False, "missing WOLFRAM_APP_ID"),
        "paper": Loadability("paper", True),
    }
    out = format_unavailable(verdicts)
    assert (
        out == "Kinds unavailable: math (missing WOLFRAM_APP_ID), patent (prohibited)."
    )


def test_format_unavailable_omits_loaded_entries() -> None:
    verdicts = {
        "paper": Loadability("paper", True),
        "patent": Loadability("patent", False, "prohibited"),
    }
    assert "paper" not in format_unavailable(verdicts)
    assert "patent" in format_unavailable(verdicts)


# ---------------------------------------------------------------------------
# server integration: _kinds_unavailable_line
# ---------------------------------------------------------------------------


def _runtime_with_loadabilities(verdicts: dict[str, Loadability]):
    """PrecisRuntime with a fake hub carrying explicit verdicts.

    The runtime's PrecisConfig is constructed with no overrides so
    no env vars leak in.
    """
    from precis.config import PrecisConfig
    from precis.runtime import PrecisRuntime

    class _FakeHub:
        kinds: set[str] = set()

        def __init__(self, loadabilities: dict[str, Loadability]) -> None:
            self.loadabilities = loadabilities

    return PrecisRuntime(
        config=PrecisConfig(),
        hub=_FakeHub(verdicts),  # type: ignore[arg-type]
    )


def test_server_kinds_unavailable_line_renders_verdicts() -> None:
    from precis.server import _kinds_unavailable_line

    rt = _runtime_with_loadabilities(
        {
            "patent": Loadability("patent", False, "prohibited"),
            "math": Loadability("math", False, "missing WOLFRAM_APP_ID"),
        }
    )
    out = _kinds_unavailable_line(rt)
    assert (
        out == "Kinds unavailable: math (missing WOLFRAM_APP_ID), patent (prohibited)."
    )


def test_server_kinds_unavailable_line_empty_for_clean_boot() -> None:
    from precis.server import _kinds_unavailable_line

    rt = _runtime_with_loadabilities({"paper": Loadability("paper", True)})
    assert _kinds_unavailable_line(rt) == ""


def test_build_instructions_includes_unavailable_line_when_relevant() -> None:
    """End-to-end: when the hub has at least one False verdict,
    the cold-start banner carries the Kinds unavailable: line.
    """
    from precis import server

    rt = _runtime_with_loadabilities(
        {
            "patent": Loadability("patent", False, "prohibited"),
        }
    )
    out = server._build_instructions(rt)
    assert "Kinds unavailable: patent (prohibited)." in out


def test_build_instructions_omits_unavailable_line_when_clean() -> None:
    """End-to-end: when every recorded verdict is loaded=True, the
    banner stays lean — no extra line, no extra bytes.
    """
    from precis import server

    rt = _runtime_with_loadabilities({"paper": Loadability("paper", True)})
    out = server._build_instructions(rt)
    assert "Kinds unavailable" not in out


# ---------------------------------------------------------------------------
# Patent convergence: env-trio reading moved into PatentHandler.__init__
# ---------------------------------------------------------------------------


def test_patent_handler_raises_init_error_when_envs_missing() -> None:
    """Convergence: with no explicit ops/raw_root and no env vars,
    PatentHandler.__init__ raises InitError with the conventional
    ``"patent: missing env vars ..."`` shape. (The kind_gate would
    normally skip before we reach here in production; this test
    exercises the defense-in-depth raise.)
    """
    from precis.dispatch import Hub, InitError
    from precis.handlers.patent import PatentHandler

    # A hub with a non-None store so the earlier "store required"
    # check passes; we want to land on the env-trio check.
    class _FakeStore:
        pass

    hub = Hub(store=_FakeStore())  # type: ignore[arg-type]

    with patch.dict(os.environ, {}, clear=False):
        for env in (
            "EPO_OPS_CLIENT_KEY",
            "EPO_OPS_CLIENT_SECRET",
            "PRECIS_PATENT_RAW_ROOT",
        ):
            os.environ.pop(env, None)
        with pytest.raises(InitError, match="patent: missing env vars"):
            PatentHandler(hub=hub)


def test_patent_handler_test_path_unaffected_by_env() -> None:
    """The test path (explicit ops= and raw_root= kwargs) continues
    to construct without touching env vars, so existing fixtures
    in test_patent_handler.py keep working.
    """
    from pathlib import Path

    from precis.dispatch import Hub
    from precis.handlers.patent import PatentHandler

    class _FakeStore:
        pass

    class _FakeOps:
        pass

    hub = Hub(store=_FakeStore())  # type: ignore[arg-type]

    with patch.dict(os.environ, {}, clear=False):
        for env in (
            "EPO_OPS_CLIENT_KEY",
            "EPO_OPS_CLIENT_SECRET",
            "PRECIS_PATENT_RAW_ROOT",
        ):
            os.environ.pop(env, None)
        # Even with the env unset, explicit kwargs skip the env path.
        h = PatentHandler(
            hub=hub,
            ops=_FakeOps(),  # type: ignore[arg-type]
            raw_root=Path("/tmp/patent-raw"),
        )
        assert h.ops is not None  # constructed without env
        assert h.raw_root == Path("/tmp/patent-raw")
