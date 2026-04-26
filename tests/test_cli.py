"""CLI surface — argument parsing and exit codes."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from precis import cli


def test_no_args_exits(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["precis"])
    with pytest.raises(SystemExit):
        cli.main()


def test_serve_invokes_server_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """`precis serve` must dispatch to precis.server.main()."""
    called: dict[str, Any] = {"hit": False}

    def fake_main() -> None:
        called["hit"] = True

    import precis.server

    monkeypatch.setattr(precis.server, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["precis", "serve"])

    cli.main()
    assert called["hit"] is True


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


def test_migrate_dry_run_against_fresh_db(
    fresh_db: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["precis", "migrate", "--database-url", fresh_db, "--dry-run"]
    )
    cli.main()
    out = capsys.readouterr().out
    assert "would apply" in out
    assert "0001_initial" in out


def test_migrate_applies_pending(
    fresh_db: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["precis", "migrate", "--database-url", fresh_db])
    cli.main()
    out = capsys.readouterr().out
    assert "applied" in out
    assert "0001_initial" in out

    # Second run is a no-op.
    monkeypatch.setattr(sys, "argv", ["precis", "migrate", "--database-url", fresh_db])
    cli.main()
    out = capsys.readouterr().out
    assert "nothing to apply" in out


def test_migrate_without_dsn_exits(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["precis", "migrate"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no database_url" in err


# ---------------------------------------------------------------------------
# jobs ingest-bundle / ingest-bundles
# ---------------------------------------------------------------------------


def _make_bundle(path: Path, *, doi: str = "10.1/x") -> None:
    data = {
        "header": {
            "title": "Sample Title On Nitrate",
            "authors": [{"name": "Wang, Q."}],
            "year": 2020,
            "doi": doi,
            "abstract": "An abstract.",
            "journal": "Nature",
        },
        "blocks": [{"text": "intro"}, {"text": "methods 5 5 5 5 5"}],
        "enrichment_meta": {},
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f)


def test_ingest_bundle_writes_to_db(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "p.acatome"
    _make_bundle(bundle)
    # Reach into the conftest to share the test DSN.
    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "precis",
            "jobs",
            "ingest-bundle",
            str(bundle),
            "--database-url",
            dsn,
        ],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "inserted" in out
    assert "wang2020" in out


def test_ingest_bundles_directory(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_bundle(tmp_path / "a.acatome", doi="10.1/a")
    _make_bundle(tmp_path / "b.acatome", doi="10.1/b")
    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "precis",
            "jobs",
            "ingest-bundles",
            str(tmp_path),
            "--database-url",
            dsn,
        ],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "inserted=2" in out
    assert "skipped=0" in out


def test_ingest_bundles_dry_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_bundle(tmp_path / "a.acatome")
    monkeypatch.setattr(
        sys,
        "argv",
        ["precis", "jobs", "ingest-bundles", str(tmp_path), "--dry-run"],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "ok=1" in out


def test_ingest_bundles_handles_corrupt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_bundle(tmp_path / "good.acatome")
    (tmp_path / "broken.acatome").write_text("not gzip")
    monkeypatch.setattr(
        sys,
        "argv",
        ["precis", "jobs", "ingest-bundles", str(tmp_path), "--dry-run"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "ok=1" in out
    assert "failed=1" in out


def test_ingest_bundle_missing_file(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["precis", "jobs", "ingest-bundle", "/no/such/file.acatome"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_dsn_from(store) -> str:
    """Pull the DSN out of a Store fixture's psycopg pool."""
    return store.pool.conninfo
