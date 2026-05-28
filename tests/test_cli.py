"""CLI surface — argument parsing and exit codes."""

from __future__ import annotations

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
# jobs import-perplexity
# ---------------------------------------------------------------------------


def test_import_perplexity_dry_run_derives_query_from_h1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run should print the derived query (from H1) per file and
    not touch any database."""
    (tmp_path / "a.md").write_text("# How does DAC work\n\nbody a\n")
    (tmp_path / "b.md").write_text("# Compare BECCS vs DAC\n\nbody b\n")
    # Headingless file — should fall back to filename.
    (tmp_path / "headingless-report.md").write_text("just a paragraph\n")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "precis",
            "jobs",
            "import-perplexity",
            str(tmp_path),
            "--dry-run",
        ],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "'How does DAC work'" in out
    assert "'Compare BECCS vs DAC'" in out
    assert "'headingless report'" in out  # filename fallback
    assert "3 file(s) would import" in out


def test_import_perplexity_filename_strategy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--query-from filename`` ignores the H1 and uses the stem."""
    (tmp_path / "some-topic.md").write_text("# Unused Heading\n\nbody\n")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "precis",
            "jobs",
            "import-perplexity",
            str(tmp_path),
            "--dry-run",
            "--query-from",
            "filename",
        ],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "'some topic'" in out
    assert "Unused Heading" not in out


def test_import_perplexity_writes_to_db(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: import two reports and verify both refs land in the
    DB under the requested kind with ``source=imported`` provenance."""
    (tmp_path / "r1.md").write_text("# Query one\n\nbody one\n")
    (tmp_path / "r2.md").write_text("# Query two\n\nbody two\n")
    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "precis",
            "jobs",
            "import-perplexity",
            str(tmp_path),
            "--kind",
            "research",
            "--database-url",
            dsn,
        ],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "imported=2" in out
    assert "failed=0" in out

    refs = store.list_refs(kind="research", provider="perplexity", limit=10)
    titles = {r.title for r in refs}
    # Titles are derived from the query via _title_for_query (capitalized).
    assert any("Query one" in t or "Query One" in t for t in titles)
    assert any("Query two" in t or "Query Two" in t for t in titles)
    for r in refs:
        assert (r.meta or {}).get("source") == "imported"


def test_import_perplexity_skips_empty_files(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty files are reported as failures but don't abort the batch."""
    (tmp_path / "ok.md").write_text("# Real query\n\nbody\n")
    (tmp_path / "empty.md").write_text("")
    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "precis",
            "jobs",
            "import-perplexity",
            str(tmp_path),
            "--database-url",
            dsn,
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1  # failures → non-zero exit
    out = capsys.readouterr().out
    assert "imported=1" in out
    assert "failed=1" in out


def test_import_perplexity_missing_dir(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["precis", "jobs", "import-perplexity", "/no/such/dir"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# ingest (unified prose-file ingest)
# ---------------------------------------------------------------------------


def test_ingest_covers_all_three_prose_kinds(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single ``precis jobs ingest`` call under PRECIS_ROOT must
    ingest every .md / .txt / .tex file under that root."""
    (tmp_path / "notes.md").write_text("# Notes\n\nbody.\n")
    (tmp_path / "log.txt").write_text("stdout line 1.\nstdout line 2.\n")
    (tmp_path / "paper.tex").write_text(r"\section{Intro}" + "\n\nbody.\n")

    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        ["precis", "jobs", "ingest", str(tmp_path), "--database-url", dsn],
    )
    cli.main()
    out = capsys.readouterr().out
    # Per-kind summary line and each kind shows an ok row.
    assert "md=1/1" in out
    assert "plaintext=1/1" in out
    assert "tex=1/1" in out
    # Each ref was inserted into the store.
    assert store.get_ref(kind="markdown", id="notes") is not None
    assert store.get_ref(kind="plaintext", id="log") is not None
    assert store.get_ref(kind="tex", id="paper") is not None


def test_ingest_mtime_gate_skips_unchanged_on_second_run(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running ``ingest`` twice against an unchanged tree must skip
    every file on the second run."""
    (tmp_path / "a.md").write_text("# A\n\nbody.\n")
    (tmp_path / "b.tex").write_text(r"\section{B}" + "\n")

    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        ["precis", "jobs", "ingest", str(tmp_path), "--database-url", dsn],
    )
    cli.main()
    capsys.readouterr()  # drop first-run output

    cli.main()
    out = capsys.readouterr().out
    # Second run reports zero new ingests but non-zero skips.
    assert "ingested=0" in out
    assert "skipped=2" in out


def test_ingest_force_re_ingests_unchanged(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` overrides the mtime/sha gates and re-embeds every
    file. Useful after swapping the embedder."""
    (tmp_path / "a.md").write_text("# A\n\nbody.\n")

    dsn = _store_dsn_from(store)
    argv = [
        "precis",
        "jobs",
        "ingest",
        str(tmp_path),
        "--database-url",
        dsn,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", argv + ["--force"])
    cli.main()
    out = capsys.readouterr().out
    # Force path counts every match as ingested, never skipped.
    assert "ingested=1" in out
    assert "skipped=0" in out


def test_ingest_scope_to_one_kind(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--kinds tex`` restricts the walk so other extensions are
    untouched even when present on disk."""
    (tmp_path / "a.md").write_text("# A\n\nbody.\n")
    (tmp_path / "b.tex").write_text(r"\section{B}" + "\n")

    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "precis",
            "jobs",
            "ingest",
            str(tmp_path),
            "--database-url",
            dsn,
            "--kinds",
            "tex",
        ],
    )
    cli.main()
    # Only the tex ref lands in the store.
    assert store.get_ref(kind="markdown", id="a") is None
    assert store.get_ref(kind="tex", id="b") is not None


def test_ingest_rejects_unknown_kind(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typo in ``--kinds`` exits with code 2 and names the valid set."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "precis",
            "jobs",
            "ingest",
            str(tmp_path),
            "--kinds",
            "bogus",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "bogus" in err
    assert "md" in err


def test_ingest_without_root_and_no_env_exits(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No positional root, no ``PRECIS_ROOT`` → exit 2 with a
    message that points at the env var."""
    monkeypatch.delenv("PRECIS_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["precis", "jobs", "ingest"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "PRECIS_ROOT" in err


def test_ingest_auto_applies_workspace_tag(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ref ingested via the CLI must carry the ``workspace``
    flag (handler contract surfaces through the CLI path too)."""
    (tmp_path / "note.md").write_text("# N\n\nbody.\n")
    (tmp_path / "log.txt").write_text("line.\n")
    (tmp_path / "p.tex").write_text(r"\section{P}" + "\n")

    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        ["precis", "jobs", "ingest", str(tmp_path), "--database-url", dsn],
    )
    cli.main()

    for kind, slug in [("markdown", "note"), ("plaintext", "log"), ("tex", "p")]:
        ref = store.get_ref(kind=kind, id=slug)
        assert ref is not None, f"{kind}:{slug} not ingested"
        assert store.has_tag(ref.id, "FLAG", "workspace"), (
            f"{kind}:{slug} missing workspace flag"
        )


def test_ingest_md_deprecation_alias_still_works(
    store,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``precis jobs ingest-md`` keeps working for one release cycle
    but prints a deprecation notice on stderr."""
    (tmp_path / "a.md").write_text("# A\n\nbody.\n")
    (tmp_path / "b.tex").write_text(r"\section{B}" + "\n")  # must NOT ingest

    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        ["precis", "jobs", "ingest-md", str(tmp_path), "--database-url", dsn],
    )
    cli.main()
    captured = capsys.readouterr()
    assert "deprecated" in captured.err.lower()
    # Scoped to markdown only.
    assert store.get_ref(kind="markdown", id="a") is not None
    assert store.get_ref(kind="tex", id="b") is None


# ---------------------------------------------------------------------------
# gripes — human-only triage dump
# ---------------------------------------------------------------------------


def _insert_gripe(store, text: str) -> int:
    """Seed a gripe ref bypassing the handler (no agent-facing read)."""
    ref = store.insert_ref(
        kind="gripe",
        slug=None,
        title=text,
        meta={},
    )
    return ref.id


def test_gripes_dump_text_format(
    store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`precis gripes` prints every live gripe in reverse-chrono order."""
    _insert_gripe(store, "first complaint")
    _insert_gripe(store, "second complaint")

    dsn = _store_dsn_from(store)
    monkeypatch.setattr(sys, "argv", ["precis", "gripes", "--database-url", dsn])
    cli.main()
    out = capsys.readouterr().out
    assert "2 gripe(s)" in out
    assert "first complaint" in out
    assert "second complaint" in out
    # Newest first by default: second complaint appears before first.
    assert out.index("second complaint") < out.index("first complaint")


def test_gripes_dump_empty(
    store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = _store_dsn_from(store)
    monkeypatch.setattr(sys, "argv", ["precis", "gripes", "--database-url", dsn])
    cli.main()
    out = capsys.readouterr().out
    assert "no live gripes" in out


def test_gripes_dump_json_one_per_line(
    store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_gripe(store, "alpha gripe")
    _insert_gripe(store, "beta gripe")

    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys, "argv", ["precis", "gripes", "--format", "json", "--database-url", dsn]
    )
    cli.main()
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    payloads = [json.loads(ln) for ln in lines]
    assert {p["text"] for p in payloads} == {"alpha gripe", "beta gripe"}
    # Every object carries id + timestamps.
    for p in payloads:
        assert isinstance(p["id"], int)
        assert p["created_at"] is not None
        assert "deleted_at" not in p  # live-only by default


def test_gripes_dump_include_deleted(
    store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--include-deleted surfaces soft-deleted tombstones."""
    live_id = _insert_gripe(store, "still here")
    dead_id = _insert_gripe(store, "tombstoned")
    store.soft_delete_ref(dead_id)

    dsn = _store_dsn_from(store)
    # Default run hides the tombstone.
    monkeypatch.setattr(sys, "argv", ["precis", "gripes", "--database-url", dsn])
    cli.main()
    default_out = capsys.readouterr().out
    assert "still here" in default_out
    assert "tombstoned" not in default_out

    # --include-deleted surfaces it with a marker.
    monkeypatch.setattr(
        sys,
        "argv",
        ["precis", "gripes", "--include-deleted", "--database-url", dsn],
    )
    cli.main()
    all_out = capsys.readouterr().out
    assert "still here" in all_out
    assert "tombstoned" in all_out
    assert "(deleted)" in all_out
    assert "live=1" in all_out
    assert "deleted=1" in all_out
    assert str(live_id) in all_out
    assert str(dead_id) in all_out


def test_gripes_dump_oldest_first_and_limit(
    store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_gripe(store, "oldest")
    _insert_gripe(store, "middle")
    _insert_gripe(store, "newest")

    dsn = _store_dsn_from(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "precis",
            "gripes",
            "--oldest-first",
            "--limit",
            "2",
            "--database-url",
            dsn,
        ],
    )
    cli.main()
    out = capsys.readouterr().out
    # Oldest-first + limit 2 → "oldest" and "middle", no "newest".
    assert "oldest" in out
    assert "middle" in out
    assert "newest" not in out
    assert out.index("oldest") < out.index("middle")


def test_gripes_dump_without_dsn_exits(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["precis", "gripes"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no database_url" in err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_dsn_from(store) -> str:
    """Pull the DSN out of a Store fixture's psycopg pool."""
    return store.pool.conninfo
