"""Plugin job_types are discovered via the ``precis.job_types``
entry-point group, registered alongside built-ins, and dispatched
via their declared ``dispatch(ctx, spec)`` callable.

These tests run without a real wheel install — they patch the
``_entry_points`` indirection in
``precis.workers.job_types`` to inject fake EPs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from precis.workers.job_types import (
    JOB_TYPE_PLUGIN_GROUP,
    JobTypeSpec,
    _reset_plugin_cache,
    get_job_type,
    known_job_types,
)


@pytest.fixture(autouse=True)
def _reset_cache_before_and_after() -> Any:
    """Plugin discovery is cached per-process. Tests inject fakes
    so we drop the cache around each test to keep them isolated."""
    _reset_plugin_cache()
    yield
    _reset_plugin_cache()


def _fake_ep(name: str, loader: Any) -> MagicMock:
    """Build a fake entry point matching ``importlib.metadata.EntryPoint``."""
    ep = MagicMock(spec=["name", "value", "load"])
    ep.name = name
    ep.value = "fake.module:thing"
    ep.load.return_value = loader
    return ep


def _patch_eps(monkeypatch: pytest.MonkeyPatch, eps: list[Any]) -> None:
    from precis.workers import job_types as jt

    def _stub(group: str) -> list[Any]:
        assert group == JOB_TYPE_PLUGIN_GROUP, f"unexpected group: {group!r}"
        return list(eps)

    monkeypatch.setattr(jt, "_entry_points", _stub)


class TestPluginDiscovery:
    """``_discover_job_type_plugins`` walks EPs, accepts a spec or a
    factory, and isolates failures."""

    def test_returns_spec_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = JobTypeSpec(
            name="plugin_demo",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="demo",
            run=lambda **_: None,
        )
        _patch_eps(monkeypatch, [_fake_ep("plugin_demo", spec)])

        assert get_job_type("plugin_demo") is spec
        assert "plugin_demo" in known_job_types()

    def test_accepts_factory_callable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = JobTypeSpec(
            name="from_factory",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
        )

        def _factory() -> JobTypeSpec:
            return spec

        _patch_eps(monkeypatch, [_fake_ep("from_factory", _factory)])

        assert get_job_type("from_factory") is spec

    def test_broken_load_is_logged_and_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        broken = MagicMock(spec=["name", "value", "load"])
        broken.name = "broken_plugin"
        broken.load.side_effect = ImportError("intentional")

        good_spec = JobTypeSpec(
            name="good_plugin",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
        )

        _patch_eps(
            monkeypatch,
            [broken, _fake_ep("good_plugin", good_spec)],
        )

        with caplog.at_level("WARNING"):
            assert get_job_type("good_plugin") is good_spec
        # Broken plugin must be logged but not raise.
        assert any("broken_plugin" in rec.message for rec in caplog.records)

    def test_wrong_type_is_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _patch_eps(
            monkeypatch,
            [_fake_ep("bogus", "this is a string, not a JobTypeSpec")],
        )

        with caplog.at_level("WARNING"):
            assert get_job_type("bogus") is None
        assert any("bogus" in rec.message for rec in caplog.records)

    def test_plugin_cannot_shadow_builtin(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        evil = JobTypeSpec(
            name="fix_gripe",  # built-in name
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
        )
        _patch_eps(monkeypatch, [_fake_ep("rogue_fix_gripe", evil)])

        with caplog.at_level("WARNING"):
            # get_job_type('fix_gripe') hits the built-in branch
            # before plugins are consulted, so the real fix_gripe
            # wins. But the warning fires during plugin discovery,
            # which happens when known_job_types() forces the cache.
            names = known_job_types()
        assert "fix_gripe" in names
        # And the built-in spec is what we get back.
        spec = get_job_type("fix_gripe")
        assert spec is not evil


class TestKnownJobTypes:
    def test_builtins_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_eps(monkeypatch, [])
        names = known_job_types()
        assert names[:2] == ["fix_gripe", "plan_tick"]

    def test_plugins_append(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = JobTypeSpec(
            name="aaa_first_alphabetically",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
        )
        _patch_eps(monkeypatch, [_fake_ep("aaa", spec)])
        names = known_job_types()
        # Built-ins keep their order; plugin appears after them
        # even though it sorts alphabetically before "fix_gripe".
        assert names[:2] == ["fix_gripe", "plan_tick"]
        assert "aaa_first_alphabetically" in names[2:]

    def test_unknown_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_eps(monkeypatch, [])
        assert get_job_type("definitely_not_a_real_job_type") is None
