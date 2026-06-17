"""Plugin ref passes discovered via the ``precis.ref_passes``
entry-point group.

Mirrors the unit-test patterns in
``test_job_type_plugins.py`` / ``test_migrate_plugin.py``:
``_entry_points`` is patched so we can inject fake plugin factories
without setting up a real wheel install. The discovery contract
is what's tested — broken factories must be logged-and-skipped,
malformed return values must be rejected, and well-shaped
factories must register cleanly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from precis.workers._plugin_passes import (
    REF_PASS_PLUGIN_GROUP,
    discover_plugin_ref_passes,
)
from precis.workers.runner import BatchResult


def _fake_ep(name: str, loader: Any) -> MagicMock:
    ep = MagicMock(spec=["name", "value", "load"])
    ep.name = name
    ep.value = "fake.module:thing"
    ep.load.return_value = loader
    return ep


def _patch_eps(monkeypatch: pytest.MonkeyPatch, eps: list[Any]) -> None:
    from precis.workers import _plugin_passes as plugin_passes

    def _stub(group: str) -> list[Any]:
        assert group == REF_PASS_PLUGIN_GROUP, f"unexpected group: {group!r}"
        return list(eps)

    monkeypatch.setattr(plugin_passes, "_entry_points", _stub)


def _good_factory(name: str = "demo_pass") -> Any:
    def _factory(store: Any, *, profile: str, args: Any) -> Any:
        def _pass(batch_size: int) -> BatchResult:
            return BatchResult(handler=name, claimed=0, ok=0, failed=0)

        return (name, _pass, frozenset({"system"}))

    return _factory


# ── Happy path ─────────────────────────────────────────────────────


class TestDiscovery:
    def test_well_shaped_factory_registers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_eps(monkeypatch, [_fake_ep("demo", _good_factory("demo_pass"))])

        passes = discover_plugin_ref_passes(
            store=object(), profile="system", args=object()
        )
        assert len(passes) == 1
        name, fn, profiles = passes[0]
        assert name == "demo_pass"
        assert callable(fn)
        assert profiles == frozenset({"system"})

    def test_factory_opts_out_with_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _opt_out(store: Any, *, profile: str, args: Any) -> Any:
            return None

        _patch_eps(monkeypatch, [_fake_ep("optout", _opt_out)])

        passes = discover_plugin_ref_passes(
            store=object(), profile="system", args=object()
        )
        assert passes == []

    def test_factory_iterable_profiles_coerced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A factory that returns a list/tuple of profiles is
        coerced to frozenset for downstream comparison."""

        def _factory(store: Any, *, profile: str, args: Any) -> Any:
            def _pass(batch_size: int) -> BatchResult:
                return BatchResult(handler="x", claimed=0, ok=0, failed=0)

            return ("x", _pass, ["system", "agent"])

        _patch_eps(monkeypatch, [_fake_ep("listy", _factory)])
        passes = discover_plugin_ref_passes(
            store=object(), profile="system", args=object()
        )
        assert len(passes) == 1
        _name, _fn, profiles = passes[0]
        assert profiles == frozenset({"system", "agent"})


# ── Failure isolation ──────────────────────────────────────────────


class TestFailureIsolation:
    def test_broken_ep_load_logged_and_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        broken = MagicMock(spec=["name", "value", "load"])
        broken.name = "broken_plugin"
        broken.load.side_effect = ImportError("intentional")

        _patch_eps(
            monkeypatch,
            [broken, _fake_ep("good", _good_factory("good_pass"))],
        )

        with caplog.at_level("WARNING"):
            passes = discover_plugin_ref_passes(
                store=object(), profile="system", args=object()
            )
        assert any("broken_plugin" in r.message for r in caplog.records)
        # The good plugin survived.
        assert any(name == "good_pass" for name, _, _ in passes)

    def test_factory_raise_logged_and_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _explode(store: Any, *, profile: str, args: Any) -> Any:
            raise RuntimeError("plugin's factory raised")

        _patch_eps(monkeypatch, [_fake_ep("explody", _explode)])
        with caplog.at_level("WARNING"):
            passes = discover_plugin_ref_passes(
                store=object(), profile="system", args=object()
            )
        assert passes == []
        assert any("explody" in r.message for r in caplog.records)

    def test_non_callable_ep_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _patch_eps(
            monkeypatch, [_fake_ep("notcallable", "I am a string, not a factory")]
        )
        with caplog.at_level("WARNING"):
            passes = discover_plugin_ref_passes(
                store=object(), profile="system", args=object()
            )
        assert passes == []

    def test_malformed_return_tuple_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _wrong_shape(store: Any, *, profile: str, args: Any) -> Any:
            return ("name_only",)  # too few elements

        _patch_eps(monkeypatch, [_fake_ep("wrong", _wrong_shape)])
        with caplog.at_level("WARNING"):
            passes = discover_plugin_ref_passes(
                store=object(), profile="system", args=object()
            )
        assert passes == []

    def test_non_callable_pass_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _bad_callable(store: Any, *, profile: str, args: Any) -> Any:
            return ("ok_name", "not_a_callable", frozenset({"system"}))

        _patch_eps(monkeypatch, [_fake_ep("bad_cb", _bad_callable)])
        with caplog.at_level("WARNING"):
            passes = discover_plugin_ref_passes(
                store=object(), profile="system", args=object()
            )
        assert passes == []

    def test_empty_ep_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_eps(monkeypatch, [])
        passes = discover_plugin_ref_passes(
            store=object(), profile="system", args=object()
        )
        assert passes == []


# ── Plumbing ───────────────────────────────────────────────────────


class TestPluming:
    def test_factory_receives_profile_and_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _factory(store: Any, *, profile: str, args: Any) -> Any:
            captured["profile"] = profile
            captured["args"] = args
            captured["store"] = store
            return None  # opt out, we just want to inspect

        _patch_eps(monkeypatch, [_fake_ep("inspect_me", _factory)])
        sentinel_store = object()
        sentinel_args = object()
        discover_plugin_ref_passes(
            store=sentinel_store, profile="system", args=sentinel_args
        )
        assert captured["profile"] == "system"
        assert captured["args"] is sentinel_args
        assert captured["store"] is sentinel_store
