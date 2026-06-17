"""``precis.migrations`` entry-point group: plugins ship their own
forward-only migrations alongside the built-in precis ones.

These tests patch the ``_entry_points`` indirection in
``precis.store.migrate`` to inject fake plugin sources. They DO
exercise the real Postgres apply path via the ``fresh_db`` fixture
so the schema mutation is end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import dict_row

from precis.store import Migrator
from precis.store.migrate import (
    MIGRATIONS_PLUGIN_GROUP,
    MigrationSource,
)
from tests.conftest import MIGRATIONS_DIR


def _fetch(dsn: str, sql: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(sql).fetchall()


def _fake_ep(name: str, loader: Any) -> MagicMock:
    ep = MagicMock(spec=["name", "value", "load"])
    ep.name = name
    ep.value = "fake.module:thing"
    ep.load.return_value = loader
    return ep


def _patch_eps(monkeypatch: pytest.MonkeyPatch, eps: list[Any]) -> None:
    from precis.store import migrate as mig

    def _stub(group: str) -> list[Any]:
        assert group == MIGRATIONS_PLUGIN_GROUP
        return list(eps)

    monkeypatch.setattr(mig, "_entry_points", _stub)


# ── Discovery ──────────────────────────────────────────────────────


class TestDiscoverSources:
    def test_builtin_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_eps(monkeypatch, [])
        sources = Migrator.discover_sources(MIGRATIONS_DIR)
        assert sources[0].plugin == "precis"
        assert sources[0].dir == MIGRATIONS_DIR
        assert len(sources) == 1

    def test_plugin_resolves_str_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        plugin_dir = tmp_path / "plugin_migrations"
        plugin_dir.mkdir()
        _patch_eps(monkeypatch, [_fake_ep("test_plugin", str(plugin_dir))])

        sources = Migrator.discover_sources(MIGRATIONS_DIR)
        assert len(sources) == 2
        assert sources[1] == MigrationSource("test_plugin", plugin_dir)

    def test_plugin_resolves_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        plugin_dir = tmp_path / "plugin_migrations"
        plugin_dir.mkdir()
        _patch_eps(monkeypatch, [_fake_ep("test_plugin", plugin_dir)])

        sources = Migrator.discover_sources(MIGRATIONS_DIR)
        assert sources[1].dir == plugin_dir

    def test_broken_plugin_logged_and_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        broken = MagicMock(spec=["name", "value", "load"])
        broken.name = "broken_plugin"
        broken.load.side_effect = ImportError("intentional")

        _patch_eps(monkeypatch, [broken])
        with caplog.at_level("WARNING"):
            sources = Migrator.discover_sources(MIGRATIONS_DIR)

        # Only the built-in survives.
        assert [s.plugin for s in sources] == ["precis"]
        assert any("broken_plugin" in r.message for r in caplog.records)

    def test_nonexistent_dir_logged_and_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        missing = tmp_path / "does-not-exist"
        _patch_eps(monkeypatch, [_fake_ep("p", str(missing))])

        with caplog.at_level("WARNING"):
            sources = Migrator.discover_sources(MIGRATIONS_DIR)
        assert [s.plugin for s in sources] == ["precis"]


# ── End-to-end apply ───────────────────────────────────────────────


class TestApplyWithPluginMigrations:
    """A plugin migration applies after the built-in set and lands
    in ``_migrations`` with the correct plugin namespace."""

    def test_plugin_migration_applies_with_correct_plugin(
        self,
        fresh_db: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        plugin_dir = tmp_path / "plugin_migs"
        plugin_dir.mkdir()
        (plugin_dir / "0001_plugin_test.sql").write_text(
            "CREATE TABLE precis_dft_smoketest (id int PRIMARY KEY);\n"
        )

        _patch_eps(
            monkeypatch,
            [_fake_ep("precis_dft", str(plugin_dir))],
        )

        sources = Migrator.discover_sources(MIGRATIONS_DIR)
        applied = Migrator(fresh_db, sources).apply_all()

        # Plugin migration must appear in the applied list.
        assert ("precis_dft", "0001_plugin_test") in applied

        # And its table exists.
        rows = _fetch(
            fresh_db,
            "SELECT tablename FROM pg_tables WHERE tablename = 'precis_dft_smoketest'",
        )
        assert len(rows) == 1

        # And the _migrations row carries the right plugin.
        rows = _fetch(
            fresh_db,
            "SELECT plugin, version FROM _migrations "
            "WHERE version = '0001_plugin_test'",
        )
        assert rows == [{"plugin": "precis_dft", "version": "0001_plugin_test"}]

    def test_plugin_migration_idempotent(
        self,
        fresh_db: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        plugin_dir = tmp_path / "plugin_migs"
        plugin_dir.mkdir()
        (plugin_dir / "0001_smoke.sql").write_text("CREATE TABLE smoke_a (id int);\n")
        _patch_eps(monkeypatch, [_fake_ep("p", str(plugin_dir))])

        sources = Migrator.discover_sources(MIGRATIONS_DIR)
        first = Migrator(fresh_db, sources).apply_all()
        second = Migrator(fresh_db, sources).apply_all()

        assert ("p", "0001_smoke") in first
        assert second == []

    def test_two_plugins_same_version_dont_collide(
        self,
        fresh_db: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Precis's ``0001_initial`` and a plugin's ``0001_X.sql`` —
        same version name, different plugin namespaces — both apply."""
        plugin_dir = tmp_path / "plugin_migs"
        plugin_dir.mkdir()
        (plugin_dir / "0001_initial.sql").write_text(
            "CREATE TABLE plugin_clash_check (id int);\n"
        )
        _patch_eps(monkeypatch, [_fake_ep("clashy_plugin", str(plugin_dir))])

        sources = Migrator.discover_sources(MIGRATIONS_DIR)
        applied = Migrator(fresh_db, sources).apply_all()

        precis_keys = [(p, v) for p, v in applied if p == "precis"]
        plugin_keys = [(p, v) for p, v in applied if p == "clashy_plugin"]

        assert ("precis", "0001_initial") in precis_keys
        assert ("clashy_plugin", "0001_initial") in plugin_keys


# ── Backwards-compat ──────────────────────────────────────────────


class TestLegacyConstructor:
    """``Migrator(dsn, migrations_dir)`` (passing a Path directly)
    still works — needed because the existing test fixtures use
    that shape."""

    def test_path_constructor_still_works(self, fresh_db: str) -> None:
        applied = Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
        assert applied[0] == ("precis", "0001_initial")
