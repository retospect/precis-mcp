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


def test_saved_hint_tail_preserves_legacy_and_permissions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if not _pg_available():
        pytest.skip("test PostgreSQL unavailable")
    name = "precis_secret_counts_" + uuid.uuid4().hex
    admin_dsn = _dsn_with_db(_active_dsn(), "postgres")
    dsn = _dsn_with_db(_active_dsn(), name)
    scratch = ROOT / ".scratch" / name
    prior = scratch / "prior"
    prior.mkdir(parents=True)
    at_tail = scratch / "at_tail"
    at_tail.mkdir()
    for path in MIGRATIONS.glob("*.sql"):
        if path.stem < TAIL:
            shutil.copy2(path, prior / path.name)
        if path.stem <= TAIL:
            shutil.copy2(path, at_tail / path.name)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        # Stable legacy fixture: replay only sealed pre-tail files. A regenerated
        # current baseline can already contain0189 and is not a pre-tail source.
        old = Migrator(dsn, prior)
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
        runner = Migrator(dsn, at_tail)
        assert runner.pending() == [("precis", TAIL)]
        monkeypatch.setattr(
            Migrator,
            "discover_sources",
            classmethod(lambda cls, directory: [MigrationSource("precis", at_tail)]),
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
        (scratch / "result.json").write_text(
            json.dumps(
                {
                    "bootstrap": "stable_pre_tail_chain",
                    "prior_pending_count": len(fresh_pending),
                    "tail_pending": [TAIL],
                    "dry_run": dry_run,
                    "applied": [TAIL],
                    "legacy_unchanged": True,
                    "signature_permissions_unchanged": True,
                    "synthetic_roundtrip": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.mark.parametrize("snapshot", ["current", "regenerated"])
def test_current_baseline_final_saved_hint_behavior(snapshot: str) -> None:
    from precis.store import schema_dump

    if not _pg_available():
        pytest.skip("test PostgreSQL unavailable")
    name = "precis_counts_baseline_" + uuid.uuid4().hex
    admin_dsn = _dsn_with_db(_active_dsn(), "postgres")
    dsn = _dsn_with_db(_active_dsn(), name)
    scratch = ROOT / ".scratch" / name
    scratch.mkdir(parents=True)
    baseline = MIGRATIONS / "baseline/schema.sql"
    if snapshot == "regenerated":
        # The complete canonical artifact must bootstrap without externally
        # installed extensions or omitting seed tables with active triggers.
        baseline = scratch / "regenerated-schema.sql"
        baseline.write_text(
            schema_dump.generate_baseline_sql(
                admin_dsn,
                MIGRATIONS,
                scratch_db="precis_counts_dump_" + uuid.uuid4().hex,
            ),
            encoding="utf-8",
        )
    baked = TAIL in dict(
        schema_dump.parse_baseline_ledger(baseline.read_text(encoding="utf-8"))
    )
    if snapshot == "regenerated":
        assert baked, "regeneration reproduction must bake0189"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT extname FROM pg_extension ORDER BY extname"
            ).fetchall() == [("plpgsql",)]
        runner = Migrator(dsn, MIGRATIONS, baseline=baseline)
        pending = runner.pending()
        assert (("precis", TAIL) in pending) == (not baked)
        applied = runner.apply_all()
        assert (("precis", TAIL) in applied) == (not baked)
        assert runner.pending() == []
        assert runner.apply_all() == []
        keyed = make_conninfo(dsn, options="-c app.secret_key=synthetic-bootstrap-key")
        value = "synthetic😀\r\n\r\nend\r"
        with psycopg.connect(keyed) as conn:
            assert conn.execute(
                "SELECT extname FROM pg_extension WHERE extname='pgcrypto'"
            ).fetchone() == ("pgcrypto",)
            conn.execute("SELECT vault.set_secret('TEST_BOOTSTRAP', %s)", (value,))
            row = conn.execute(
                "SELECT hint FROM vault.list() WHERE name='TEST_BOOTSTRAP'"
            ).fetchone()
            assert row is not None
            assert row[0].endswith(f" · {len(value)} chars · 4 lines")
            assert value not in row[0]
            assert conn.execute("SELECT vault.reveal('TEST_BOOTSTRAP')").fetchone() == (
                value,
            )
        (scratch / "result.json").write_text(
            json.dumps(
                {
                    "snapshot": snapshot,
                    "tail_baked": baked,
                    "tail_pending": ("precis", TAIL) in pending,
                    "tail_applied": ("precis", TAIL) in applied,
                    "final_saved_counts": True,
                    "exact_roundtrip": True,
                    "idempotent": True,
                    "pgcrypto_preprovisioned": False,
                    "regenerated_scope": "canonical schema+ledger+all seed tables"
                    if snapshot == "regenerated"
                    else "current actual baseline",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
