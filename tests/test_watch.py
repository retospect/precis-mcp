"""Tests for ``precis.cli.watch``.

Filesystem helpers (``_wait_stable``, ``_move_to``,
``_move_to_corpus``, ``_write_error``, ``_should_skip``) are
plain unit tests — fast, no DB, no observer.

The orchestration (``process_pdf``, ``_handle_success``,
``_handle_failure``) is exercised with a stubbed
:func:`precis.ingest.add.precis_add` so we don't pull Marker /
Postgres into watcher tests. End-to-end watcher coverage with
the real ``precis_add`` lives in ``tests/ingest/test_add.py``.

The CLI parser registration is checked via the same ``--help``
smoke test the existing ``test_cli.py`` uses for ``add``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from precis.cli.watch import (
    _is_pdf,
    _move_to,
    _move_to_corpus,
    _should_skip,
    _wait_stable,
    _write_error,
    process_pdf,
)
from precis.ingest.add import IngestResult

# ---------------------------------------------------------------------------
# _is_pdf / _should_skip — pure
# ---------------------------------------------------------------------------


class TestIsPdf:
    def test_lowercase_pdf(self):
        assert _is_pdf(Path("paper.pdf"))

    def test_uppercase_pdf(self):
        assert _is_pdf(Path("paper.PDF"))

    def test_mixed_case(self):
        assert _is_pdf(Path("paper.Pdf"))

    def test_other_suffix(self):
        assert not _is_pdf(Path("paper.txt"))
        assert not _is_pdf(Path("paper.acatome"))


class TestShouldSkip:
    def test_inside_errors_dir(self, tmp_path: Path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        (watch_dir / "errors").mkdir()
        target = watch_dir / "errors" / "20240101-120000" / "bad.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x")
        assert _should_skip(target, watch_dir) is True

    def test_inside_completed_dir(self, tmp_path: Path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        (watch_dir / "completed").mkdir()
        target = watch_dir / "completed" / "ok.pdf"
        target.write_bytes(b"x")
        assert _should_skip(target, watch_dir) is True

    def test_normal_path_not_skipped(self, tmp_path: Path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        target = watch_dir / "subdir" / "fresh.pdf"
        target.parent.mkdir()
        target.write_bytes(b"x")
        assert _should_skip(target, watch_dir) is False

    def test_outside_watch_dir_skipped(self, tmp_path: Path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        outside = tmp_path / "elsewhere" / "stray.pdf"
        outside.parent.mkdir()
        outside.write_bytes(b"x")
        assert _should_skip(outside, watch_dir) is True


# ---------------------------------------------------------------------------
# _wait_stable
# ---------------------------------------------------------------------------


class TestWaitStable:
    def test_stable_file_returns_true(self, tmp_path: Path):
        f = tmp_path / "a.pdf"
        f.write_bytes(b"hello")
        assert _wait_stable(f, debounce=0.01) is True

    def test_missing_file_returns_false(self, tmp_path: Path):
        f = tmp_path / "ghost.pdf"
        assert _wait_stable(f, debounce=0.01) is False


# ---------------------------------------------------------------------------
# _move_to / _move_to_corpus / _write_error
# ---------------------------------------------------------------------------


class TestMoveTo:
    def test_move_basic(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        src.write_bytes(b"data")
        dest_dir = tmp_path / "dest"
        result = _move_to(src, dest_dir)
        assert result.parent == dest_dir
        assert result.name == "paper.pdf"
        assert not src.exists()
        assert result.read_bytes() == b"data"

    def test_move_creates_parent(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        src.write_bytes(b"x")
        dest_dir = tmp_path / "deep" / "nested" / "dest"
        result = _move_to(src, dest_dir)
        assert result.exists()

    def test_move_conflict_renames_with_timestamp(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        src.write_bytes(b"new")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        (dest_dir / "paper.pdf").write_bytes(b"old")
        result = _move_to(src, dest_dir)
        assert result.parent == dest_dir
        assert result.name != "paper.pdf"
        assert result.name.startswith("paper_") and result.name.endswith(".pdf")
        # Old file untouched.
        assert (dest_dir / "paper.pdf").read_bytes() == b"old"


class TestMoveToCorpus:
    def test_letter_sharded(self, tmp_path: Path):
        pdf = tmp_path / "tmp.pdf"
        pdf.write_bytes(b"x")
        corpus = tmp_path / "corpus"
        result = _move_to_corpus(pdf, cite_key="smith24", corpus_dir=corpus)
        assert result == corpus / "s" / "smith24.pdf"
        assert result.exists()
        assert not pdf.exists()

    def test_uppercase_first_letter_lowered(self, tmp_path: Path):
        pdf = tmp_path / "in.pdf"
        pdf.write_bytes(b"x")
        corpus = tmp_path / "corpus"
        result = _move_to_corpus(pdf, cite_key="Wei24a", corpus_dir=corpus)
        assert result.parent.name == "w"

    def test_non_alpha_first_char_falls_back_to_underscore(self, tmp_path: Path):
        pdf = tmp_path / "in.pdf"
        pdf.write_bytes(b"x")
        corpus = tmp_path / "corpus"
        result = _move_to_corpus(pdf, cite_key="_anon23", corpus_dir=corpus)
        assert result.parent.name == "_"

    def test_existing_destination_renamed(self, tmp_path: Path):
        corpus = tmp_path / "corpus"
        (corpus / "s").mkdir(parents=True)
        (corpus / "s" / "smith24.pdf").write_bytes(b"old")

        pdf = tmp_path / "in.pdf"
        pdf.write_bytes(b"new")

        result = _move_to_corpus(pdf, cite_key="smith24", corpus_dir=corpus)
        # Conflict resolution preserves the existing file.
        assert result.name != "smith24.pdf"
        assert result.name.startswith("smith24_")
        assert (corpus / "s" / "smith24.pdf").read_bytes() == b"old"


class TestWriteError:
    def test_writes_error_file_with_traceback(self, tmp_path: Path):
        errors_dir = tmp_path / "errors"
        errors_dir.mkdir()
        pdf = tmp_path / "bad.pdf"
        pdf.write_bytes(b"x")
        try:
            raise ValueError("ingest blew up")
        except ValueError as e:
            err_path = _write_error(errors_dir, pdf, e)

        assert err_path.exists()
        assert err_path.name == "bad.error.txt"
        text = err_path.read_text()
        assert "bad.pdf" in text
        assert "ingest blew up" in text
        assert "Traceback" in text
        assert "Time:" in text


# ---------------------------------------------------------------------------
# process_pdf — orchestration with stubbed precis_add
# ---------------------------------------------------------------------------


class TestProcessPdf:
    def _layout(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        watch_dir = tmp_path / "inbox"
        watch_dir.mkdir()
        errors_dir = watch_dir / "errors"
        duplicates_dir = errors_dir / "duplicates"
        errors_dir.mkdir()
        duplicates_dir.mkdir()
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        return watch_dir, errors_dir, duplicates_dir, corpus_dir

    def test_inserted_path_moves_to_corpus(self, tmp_path: Path):
        watch_dir, errors_dir, duplicates_dir, corpus_dir = self._layout(tmp_path)
        pdf = watch_dir / "smith2024.pdf"
        pdf.write_bytes(b"%PDF-1.4 test bytes")

        fake_result = IngestResult(
            ref_id=42,
            inserted=True,
            paper_id="aabbccdd",
            pub_id="doi:10.1/x",
            cite_key="smith24",
            pdf_sha256="a" * 64,
            content_hash="b" * 64,
            chunks_written=3,
            identifiers={"doi": "10.1/x", "cite_key": "smith24"},
        )

        with patch("precis.cli.watch.precis_add", return_value=fake_result):
            dest = process_pdf(
                pdf,
                store=object(),  # type: ignore[arg-type]  # stubbed precis_add ignores it
                corpus_dir=corpus_dir,
                errors_dir=errors_dir,
                duplicates_dir=duplicates_dir,
                debounce=0.01,
                user="reto",
            )

        assert dest is not None
        assert dest == corpus_dir / "s" / "smith24.pdf"
        assert dest.exists()
        assert not pdf.exists()  # moved out of inbox

        # ingest.log written with correct columns.
        log_lines = (corpus_dir / "ingest.log").read_text().splitlines()
        assert len(log_lines) == 1
        cols = log_lines[0].split("\t")
        assert cols[1] == "reto"
        assert cols[2] == "smith24"
        assert cols[3] == "42"
        assert cols[4] == "inserted"
        assert cols[5] == "smith2024.pdf"

    def test_existed_path_moves_to_duplicates(self, tmp_path: Path):
        watch_dir, errors_dir, duplicates_dir, corpus_dir = self._layout(tmp_path)
        pdf = watch_dir / "dup.pdf"
        pdf.write_bytes(b"%PDF dup")

        fake_result = IngestResult(
            ref_id=7,
            inserted=False,
            paper_id="ee55ff66",
            pub_id="doi:10.1/y",
            cite_key="jones23",
            pdf_sha256=None,
            content_hash=None,
            chunks_written=0,
            identifiers={"doi": "10.1/y", "cite_key": "jones23"},
        )

        with patch("precis.cli.watch.precis_add", return_value=fake_result):
            dest = process_pdf(
                pdf,
                store=object(),  # type: ignore[arg-type]
                corpus_dir=corpus_dir,
                errors_dir=errors_dir,
                duplicates_dir=duplicates_dir,
                debounce=0.01,
                user="reto",
            )

        assert dest is not None
        assert dest.parent == duplicates_dir
        assert not pdf.exists()
        # No corpus copy.
        assert not (corpus_dir / "j" / "jones23.pdf").exists()
        # Log line records ``existed`` not ``inserted``.
        log_text = (corpus_dir / "ingest.log").read_text()
        assert "\texisted\t" in log_text

    def test_failure_path_moves_to_errors_with_traceback(self, tmp_path: Path):
        watch_dir, errors_dir, duplicates_dir, corpus_dir = self._layout(tmp_path)
        pdf = watch_dir / "broken.pdf"
        pdf.write_bytes(b"%PDF broken")

        with patch(
            "precis.cli.watch.precis_add",
            side_effect=ValueError("marker exploded"),
        ):
            dest = process_pdf(
                pdf,
                store=object(),  # type: ignore[arg-type]
                corpus_dir=corpus_dir,
                errors_dir=errors_dir,
                duplicates_dir=duplicates_dir,
                debounce=0.01,
                user="reto",
            )

        # Failure path returns None, the watcher loop survives.
        assert dest is None
        # Original PDF moved into a timestamped bucket under errors/.
        assert not pdf.exists()
        ts_buckets = [
            p for p in errors_dir.iterdir() if p.is_dir() and p.name != "duplicates"
        ]
        assert len(ts_buckets) == 1
        bucket = ts_buckets[0]
        assert (bucket / "broken.pdf").exists()
        # Sibling .error.txt with traceback.
        err_files = list(bucket.glob("*.error.txt"))
        assert len(err_files) == 1
        assert "marker exploded" in err_files[0].read_text()
        # Failure does NOT write to ingest.log.
        assert not (corpus_dir / "ingest.log").exists()

    def test_disappearing_file_returns_none(self, tmp_path: Path):
        watch_dir, errors_dir, duplicates_dir, corpus_dir = self._layout(tmp_path)
        pdf = watch_dir / "ghost.pdf"
        # Don't write — file doesn't exist.
        with patch("precis.cli.watch.precis_add") as m:
            dest = process_pdf(
                pdf,
                store=object(),  # type: ignore[arg-type]
                corpus_dir=corpus_dir,
                errors_dir=errors_dir,
                duplicates_dir=duplicates_dir,
                debounce=0.01,
                user="",
            )
        assert dest is None
        m.assert_not_called()  # never reached precis_add


# ---------------------------------------------------------------------------
# CLI parser smoke test
# ---------------------------------------------------------------------------


class TestParserRegistration:
    def test_watch_in_top_level_help(self):
        from precis.cli import _build_parser

        parser = _build_parser()
        actions = {getattr(action, "dest", None) for action in parser._actions}
        # The subparsers action's choices are the registered subcommands.
        sub_actions = [action for action in parser._actions if action.dest == "cmd"]
        assert sub_actions, "expected a subparsers action with dest='cmd'"
        choices = sub_actions[0].choices  # type: ignore[attr-defined]
        assert "watch" in choices
        assert "add" in choices  # sanity — B4 still registered

    def test_watch_help_renders(self, capsys):
        from precis.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["watch", "--help"])
        out = capsys.readouterr().out
        assert "watch_dir" in out
        assert "--corpus-dir" in out
        assert "--no-backfill" in out
