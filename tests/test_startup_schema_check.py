"""Startup schema gate tests (``precis.store.migrate.check_schema_at_startup``).

A ``pip install -U`` must never silently run new code on an old schema:
every long-running entrypoint calls the gate once at boot — refuse when
the packaged migrations are ahead of the ``_migrations`` ledger, warn +
continue when the ledger is ahead of the package (rolling deploys),
optionally self-heal under ``PRECIS_AUTO_MIGRATE`` serialized on a
Postgres advisory lock, and always honour the ``PRECIS_SKIP_SCHEMA_CHECK``
escape hatch.

Each test gets an ephemeral database via ``fresh_db`` (schema stripped;
the fixture's teardown re-applies every migration for the next test).
"""

from __future__ import annotations

import argparse
import threading

import psycopg
import pytest

from precis.store import Migrator
from precis.store.migrate import (
    MigrationSource,
    check_schema_at_startup,
    packaged_migrator,
)
from tests.conftest import MIGRATIONS_DIR

#: The ledger row tests delete to fabricate "one pending migration".
#: 0207 is fully ``IF [NOT] EXISTS``-guarded, so the fixture teardown's
#: ledger-idempotent ``apply_all`` re-stamps it without error.
_HEAD_VERSION = "0207_nanopub_supersessions"


def _apply_all(dsn: str) -> None:
    """Bring ``fresh_db`` to the packaged head (conftest restore sources)."""
    Migrator(dsn, Migrator.discover_sources(MIGRATIONS_DIR)).apply_all()


def _unapply_head(dsn: str) -> None:
    """Drop the head ledger row → exactly one pending migration."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM public._migrations WHERE plugin = 'precis' AND version = %s",
            (_HEAD_VERSION,),
        )


def test_refuses_when_pending_and_auto_migrate_off(
    fresh_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _apply_all(fresh_db)
    _unapply_head(fresh_db)
    with pytest.raises(SystemExit) as exc:
        check_schema_at_startup(fresh_db, auto_migrate=False)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "1 migration(s) behind" in err
    assert f"precis/{_HEAD_VERSION}" in err
    assert "precis migrate" in err


def test_starts_clean_when_current(fresh_db: str) -> None:
    _apply_all(fresh_db)
    check_schema_at_startup(fresh_db, auto_migrate=False)  # no raise


def test_ahead_of_package_warns_and_continues(
    fresh_db: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A rolling-deploy peer applied a migration this build doesn't ship —
    ledger version sorts past the packaged head → warn, keep booting."""
    _apply_all(fresh_db)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO public._migrations (plugin, version, checksum) "
            "VALUES ('precis', '9999_ahead_probe', 'x')"
        )
    try:
        with caplog.at_level("WARNING"):
            check_schema_at_startup(fresh_db, auto_migrate=False)  # no raise
        assert any("AHEAD" in r.message for r in caplog.records)
        assert any("9999_ahead_probe" in r.message for r in caplog.records)
    finally:
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM public._migrations WHERE version = '9999_ahead_probe'"
            )


def test_auto_migrate_applies_and_starts(fresh_db: str) -> None:
    _apply_all(fresh_db)
    _unapply_head(fresh_db)
    check_schema_at_startup(fresh_db, auto_migrate=True)
    assert packaged_migrator(fresh_db).schema_status().pending == []


def test_auto_migrate_env_var_enables(fresh_db: str, monkeypatch) -> None:
    """``PRECIS_AUTO_MIGRATE=1`` resolves through the settings registry
    when no explicit flag is passed."""
    _apply_all(fresh_db)
    _unapply_head(fresh_db)
    monkeypatch.setenv("PRECIS_AUTO_MIGRATE", "1")
    check_schema_at_startup(fresh_db)
    assert packaged_migrator(fresh_db).schema_status().pending == []


def test_two_concurrent_auto_migrators_apply_once(fresh_db: str, tmp_path) -> None:
    """The advisory lock serializes concurrent starters: a non-idempotent
    probe migration would raise ``DuplicateTable`` on a second apply, so
    two racers finishing cleanly proves the lock did its job."""
    _apply_all(fresh_db)
    probe_dir = tmp_path / "probe_migrations"
    probe_dir.mkdir()
    (probe_dir / "9001_probe.sql").write_text(
        "CREATE TABLE probe_applied_once (n int);\n", encoding="utf-8"
    )
    migrator = Migrator(
        fresh_db,
        [
            MigrationSource("precis", MIGRATIONS_DIR),
            MigrationSource("probe", probe_dir),
        ],
    )
    barrier = threading.Barrier(2)
    results: list[str] = []

    def race() -> None:
        try:
            barrier.wait(timeout=10)
            check_schema_at_startup(fresh_db, auto_migrate=True, migrator=migrator)
            results.append("ok")
        except Exception as exc:  # report, don't raise inside the thread
            results.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=race) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert results == ["ok", "ok"]
        with psycopg.connect(fresh_db) as conn:
            n_row = conn.execute(
                "SELECT count(*) FROM public._migrations WHERE plugin = 'probe'"
            ).fetchone()
            table_row = conn.execute(
                "SELECT to_regclass('public.probe_applied_once')"
            ).fetchone()
        assert n_row is not None and table_row is not None
        n, table = n_row[0], table_row[0]
        assert n == 1
        assert table == "probe_applied_once"
    finally:
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute("DELETE FROM public._migrations WHERE plugin = 'probe'")
            conn.execute("DROP TABLE IF EXISTS public.probe_applied_once")


def test_skip_check_env_bypasses(
    fresh_db: str, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``PRECIS_SKIP_SCHEMA_CHECK=1`` boots anyway, loudly."""
    _apply_all(fresh_db)
    _unapply_head(fresh_db)
    monkeypatch.setenv("PRECIS_SKIP_SCHEMA_CHECK", "1")
    with caplog.at_level("WARNING"):
        check_schema_at_startup(fresh_db)  # no raise despite pending
    assert any("PRECIS_SKIP_SCHEMA_CHECK" in r.message for r in caplog.records)


def test_gate_warn_logs_and_continues(
    fresh_db: str, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``PRECIS_SCHEMA_GATE=warn`` serves a behind-schema DB loudly —
    the session-MCP mode: it must stay up through the window between a
    deploy publishing new code and the owning host's ``precis migrate``."""
    _apply_all(fresh_db)
    _unapply_head(fresh_db)
    monkeypatch.setenv("PRECIS_SCHEMA_GATE", "warn")
    with caplog.at_level("WARNING"):
        check_schema_at_startup(fresh_db)  # no raise despite pending
    assert any(
        "1 migration(s) behind" in r.message and "PRECIS_SCHEMA_GATE=warn" in r.message
        for r in caplog.records
    )
    # warn never migrates — the ledger row stays deleted.
    assert len(packaged_migrator(fresh_db).schema_status().pending) == 1


def test_gate_warn_auto_migrate_still_wins(fresh_db: str, monkeypatch) -> None:
    """``PRECIS_AUTO_MIGRATE=1`` applies under warn mode too — the gate
    chooses how to *react* to pending, auto-migrate is the stronger ask."""
    _apply_all(fresh_db)
    _unapply_head(fresh_db)
    monkeypatch.setenv("PRECIS_SCHEMA_GATE", "warn")
    monkeypatch.setenv("PRECIS_AUTO_MIGRATE", "1")
    check_schema_at_startup(fresh_db)
    assert packaged_migrator(fresh_db).schema_status().pending == []


def test_gate_off_skips_check(
    fresh_db: str, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``PRECIS_SCHEMA_GATE=off`` is ``PRECIS_SKIP_SCHEMA_CHECK`` spelled
    as a mode — it returns before the ledger is even read."""
    _apply_all(fresh_db)
    _unapply_head(fresh_db)
    monkeypatch.setenv("PRECIS_SCHEMA_GATE", "off")
    with caplog.at_level("WARNING"):
        check_schema_at_startup(fresh_db)  # no raise despite pending
    assert any("PRECIS_SCHEMA_GATE=off" in r.message for r in caplog.records)


def test_gate_invalid_value_is_a_boot_error(
    fresh_db: str, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd ``PRECIS_SCHEMA_GATE`` must fail closed with a clear
    error — never silently degrade to refuse or warn."""
    _apply_all(fresh_db)
    _unapply_head(fresh_db)
    monkeypatch.setenv("PRECIS_SCHEMA_GATE", "warn-ish")
    with pytest.raises(SystemExit) as exc:
        check_schema_at_startup(fresh_db)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "PRECIS_SCHEMA_GATE='warn-ish' is invalid" in err
    assert "refuse" in err and "warn" in err and "off" in err


def test_migrate_subcommand_not_blocked(fresh_db: str) -> None:
    """``precis migrate`` never hits the gate — it IS the fix."""
    _apply_all(fresh_db)
    _unapply_head(fresh_db)
    from precis.cli import migrate as migrate_cli

    migrate_cli.run(
        argparse.Namespace(database_url=fresh_db, dry_run=False, from_scratch=False)
    )
    assert packaged_migrator(fresh_db).schema_status().pending == []
