"""Real throwaway-DB checks for saved counts through the sealed vault seam."""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from precis.cli import migrate as migrate_cli
from precis.store import Migrator
from precis.store.migrate import MigrationSource
from tests.conftest import _active_dsn, _dsn_with_db, _pg_available

ROOT = Path(__file__).parents[1]
MIGRATIONS = ROOT / "src/precis/migrations"
TAIL = "0189_secret_hint_counts"
pytestmark = pytest.mark.db


@pytest.mark.parametrize("bootstrap", ["chain", "baseline"])
def test_saved_hint_tail_preserves_legacy_and_permissions(
    bootstrap: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if not _pg_available():
        pytest.skip("test PostgreSQL unavailable")
    name = "precis_secret_counts_" + uuid.uuid4().hex
    admin_dsn = _dsn_with_db(_active_dsn(), "postgres")
    dsn = _dsn_with_db(_active_dsn(), name)
    scratch = ROOT / ".scratch" / name
    prior = scratch / "prior"
    prior.mkdir(parents=True)
    for path in MIGRATIONS.glob("*.sql"):
        if path.stem != TAIL:
            shutil.copy2(path, prior / path.name)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        baseline = (
            MIGRATIONS / "baseline/schema.sql" if bootstrap == "baseline" else None
        )
        # Match the vault test prerequisite: the current generated baseline
        # omits pgcrypto, while the numbered chain provisions it in 0059.
        with psycopg.connect(dsn) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        old = Migrator(dsn, prior, baseline=baseline)
        fresh_pending = old.pending()
        old.apply_all()
        assert not old.pending()
        keyed = make_conninfo(
            dsn, options="-c app.secret_key=synthetic-migration-test-key"
        )
        with psycopg.connect(keyed) as conn:
            conn.execute(
                "SELECT vault.set_secret('TEST_LEGACY', 'synthetic-before-tail')"
            )
            before = conn.execute(
                "SELECT ciphertext,hint,updated_at FROM vault.secrets WHERE name='TEST_LEGACY'"
            ).fetchone()
            assert before and " chars · " not in before[1]
            signature = conn.execute(
                "SELECT oid,proowner,proacl,provolatile,prosecdef FROM pg_proc WHERE oid='vault._hint(text)'::regprocedure"
            ).fetchone()
        runner = Migrator(dsn, MIGRATIONS)
        assert runner.pending() == [("precis", TAIL)]
        monkeypatch.setattr(
            Migrator,
            "discover_sources",
            classmethod(lambda cls, directory: [MigrationSource("precis", directory)]),
        )
        migrate_cli.run(
            argparse.Namespace(database_url=dsn, dry_run=True, from_scratch=False)
        )
        dry_run = capsys.readouterr().out
        assert "would apply 1 migration(s)" in dry_run and TAIL in dry_run
        assert runner.pending() == [("precis", TAIL)]
        assert runner.apply_all() == [("precis", TAIL)]
        assert runner.apply_all() == []
        with psycopg.connect(keyed) as conn:
            assert (
                conn.execute(
                    "SELECT ciphertext,hint,updated_at FROM vault.secrets WHERE name='TEST_LEGACY'"
                ).fetchone()
                == before
            )
            assert (
                conn.execute(
                    "SELECT oid,proowner,proacl,provolatile,prosecdef FROM pg_proc WHERE oid='vault._hint(text)'::regprocedure"
                ).fetchone()
                == signature
            )
            assert conn.execute(
                "SELECT hint FROM vault.list() WHERE name='TEST_LEGACY'"
            ).fetchone() == (before[1],)
            assert conn.execute("SELECT vault.mask('TEST_LEGACY')").fetchone() == (
                before[1],
            )
            replacement = "synthetic😀\r\n\r\nend\r"
            conn.execute("SELECT vault.set_secret('TEST_LEGACY', %s)", (replacement,))
            saved = conn.execute(
                "SELECT hint FROM vault.list() WHERE name='TEST_LEGACY'"
            ).fetchone()
            assert saved is not None
            assert saved[0].endswith(f" · {len(replacement)} chars · 4 lines")
            assert conn.execute("SELECT vault.reveal('TEST_LEGACY')").fetchone() == (
                replacement,
            )
        # A truly fresh bootstrap must replay this new tail after the snapshot.
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        with psycopg.connect(dsn) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        fresh = Migrator(dsn, MIGRATIONS, baseline=baseline)
        assert ("precis", TAIL) in fresh.pending()
        applied = fresh.apply_all()
        assert ("precis", TAIL) in applied
        with psycopg.connect(dsn) as conn:
            hint = conn.execute("SELECT vault._hint(E'A\\r\\nB\\n')").fetchone()
            assert hint is not None
            assert hint[0].endswith(" · 5 chars · 3 lines")
        (scratch / "result.json").write_text(
            json.dumps(
                {
                    "bootstrap": bootstrap,
                    "prior_pending_count": len(fresh_pending),
                    "tail_pending": [TAIL],
                    "dry_run": dry_run,
                    "applied": [TAIL],
                    "legacy_unchanged": True,
                    "signature_permissions_unchanged": True,
                    "fresh_tail_applied": True,
                    "synthetic_roundtrip": True,
                },
                indent=2,
            )
        )
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
