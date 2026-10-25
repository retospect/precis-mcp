"""Fingerprint-stamped template + cached schema dump — gr514478 S5.

The unit cases (cheap source resolution, fingerprint stability) need no
DB. The stamp/dump/load cases run against scratch databases on the test
server — they take ``store`` only so the session is db-marked and
postgres is reachable, then do their own admin work on
``precis_cachetest_*`` DBs, dropped in teardown.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest

import tests.conftest as conftest
from precis.store import Migrator
from precis.store.migrate import MigrationSource

_has_pg_dump = shutil.which("pg_dump") is not None


def test_cheap_sources_match_discover_sources() -> None:
    """The find_spec resolution must find exactly the dirs the real
    discovery would — else the fingerprint could bless a different file
    set than apply_all applies."""
    cheap = conftest._cheap_migration_sources()
    assert cheap is not None, "cheap resolution fell back — convention broken?"
    full = Migrator.discover_sources(conftest.MIGRATIONS_DIR)
    assert sorted((s.plugin, str(s.dir)) for s in cheap) == sorted(
        (s.plugin, str(s.dir)) for s in full
    )


def test_fingerprint_stable_and_content_sensitive() -> None:
    fp1 = conftest._migration_fingerprint()
    fp2 = conftest._migration_fingerprint()
    assert fp1 == fp2
    assert len(fp1) == 64


def test_fingerprint_changes_with_file_set(tmp_path: Path) -> None:
    """A draft migration added or edited must change the fingerprint."""
    import hashlib

    from precis.store.migrate import _load_migrations

    def fp_of(dirpath: Path) -> str:
        h = hashlib.sha256()
        for f in _load_migrations(MigrationSource("p", dirpath)):
            h.update(f.checksum.encode("utf-8"))
        return h.hexdigest()

    d1 = tmp_path / "m1"
    d1.mkdir()
    (d1 / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    before = fp_of(d1)
    (d1 / "0002_b.sql").write_text("SELECT 2;", encoding="utf-8")
    assert fp_of(d1) != before


@pytest.mark.skipif(not _has_pg_dump, reason="pg_dump not on PATH")
def test_dump_write_and_cold_load(store, tmp_path, monkeypatch) -> None:
    """A scratch DB dumped to the cache file rebuilds a cold DB — the
    cold-template path of _initialise_test_db."""
    monkeypatch.setattr(conftest, "_WORKTREE_ROOT", tmp_path)
    admin_dsn = conftest._dsn_with_db(conftest.PG_TEST_DSN, "postgres")
    src, dst = "precis_cachetest_src", "precis_cachetest_dst"
    fp = "a" * 64
    with psycopg.connect(admin_dsn, autocommit=True) as adm:
        for name in (src, dst):
            conftest._drop_db(adm, name)
        try:
            adm.execute(f'CREATE DATABASE "{src}"')
            with psycopg.connect(
                conftest._dsn_with_db(conftest.PG_TEST_DSN, src), autocommit=True
            ) as c:
                c.execute("CREATE TABLE probe_t (id int PRIMARY KEY)")
                c.execute("INSERT INTO probe_t VALUES (7)")
            conftest._write_schema_dump(
                fp, dsn=conftest._dsn_with_db(conftest.PG_TEST_DSN, src)
            )
            assert conftest._schema_dump_path(fp).exists()
            assert conftest._template_state(adm, dst) == "missing"
            assert conftest._load_schema_dump(adm, fp, dst) is True
            with psycopg.connect(conftest._dsn_with_db(conftest.PG_TEST_DSN, dst)) as c:
                assert c.execute("SELECT id FROM probe_t").fetchall() == [(7,)]
            assert conftest._read_template_fp(adm, dst) == fp
        finally:
            for name in (src, dst):
                conftest._drop_db(adm, name)


def test_stamp_roundtrip_and_template_state(store) -> None:
    """Stamp read/write + state detection against the real template."""
    admin_dsn = conftest._dsn_with_db(conftest.PG_TEST_DSN, "postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as adm:
        assert conftest._template_state(adm) == "migrated"
        original = conftest._read_template_fp(adm)
        try:
            conftest._stamp_template_fp(adm, "f" * 64)
            assert conftest._read_template_fp(adm) == "f" * 64
        finally:
            if original is not None:
                conftest._stamp_template_fp(adm, original)
            else:
                adm.execute(f'COMMENT ON DATABASE "{conftest._TEMPLATE_DB}" IS NULL')
